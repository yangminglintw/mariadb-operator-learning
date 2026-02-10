# Slow Query Log 監控腳本

## 背景

已啟用 MariaDB slow query log，超過 `long_query_time` 門檻的 SQL 會寫入容器內的 log 檔案。
這組腳本從容器取出並分析慢查詢記錄。

**為什麼 Replica 也要查**：
- 讀取流量透過 MaxScale 分散到 Replica，slow SELECT 會記錄在 Replica 的 slow log
- Replication SQL thread 如果慢（大 transaction replay），也會記錄
- Primary 的 slow log 只有打到 Primary 的查詢，不包含 Replica 收到的讀取流量

---

## 效能安全策略

### 容器內操作分級

| 操作 | 在容器內？ | 影響 | 用途 |
|------|-----------|------|------|
| `SHOW VARIABLES` | 是 | 極低 — 讀記憶體 | 取 log 路徑 |
| `ls -lh` / `wc -l` | 是 | 極低 | 檢查檔案大小 |
| `tail -n N` | 是 | 低 — 只讀尾端 | 短期查詢（最近 N 行） |
| `cat` 整檔 pipe 到本地 | 是（streaming） | 中 — 順序 I/O | 長期查詢（7/14 天） |
| `awk`/`sort`/`grep` | **否 — 本地** | 零 | 所有文字解析 |

### 關鍵設計：streaming cat vs in-container awk

```
方案 A（推薦）：容器 cat → pipe → 本地 awk
  kubectl exec ... -- cat $LOG_FILE | awk -v start=... -v end=... '{filter}'
  ├── 容器：只做順序讀取（cat），CPU 近零
  ├── 本地：awk 做所有過濾和解析
  └── 適合：7/14 天的長時間範圍

方案 B：容器內 awk（避免）
  kubectl exec ... -- awk -v start=... '{filter}' $LOG_FILE
  ├── 容器：awk 消耗 CPU（解析每一行）
  └── 對 DB 效能有影響
```

**為什麼 streaming cat 可以接受**：
- `cat` 只做順序讀取，不佔 CPU、不需記憶體（streaming）
- SSD 上順序讀取很快，不會阻塞隨機 I/O（DB 主要 workload）
- 但檔案太大時（> 200MB），建議先用 `tail` 或考慮 log rotation

### 短期 vs 長期查詢策略

| 查詢範圍 | 方法 | 容器操作 |
|----------|------|---------|
| 最近 N 行 | `tail -n N` pipe 到本地 awk | `tail` only |
| 1 天 | `tail -n` 估算行數 pipe 到本地 awk | `tail` only |
| 7/14 天 | `cat` 整檔 pipe 到本地 awk | `cat` (streaming) |

### 為什麼用 awk（本地）

現有腳本（`get_sql_by_digest.sh`、`analyze_sql.sh`）不用 awk，因為資料來源是 SQL 查詢結果（已結構化）。
Slow query log 是多行文字格式（`# Time:` + `# Query_time:` + SQL），需要文字解析 → awk 是標準工具。

**關鍵**：awk 跑在 `|` pipe 的本地端，不在容器內。容器只負責 `cat`/`tail` 輸出原始文字。

**POSIX 相容**：腳本只用 POSIX awk 語法（不依賴 gawk），macOS/Linux 都能直接執行。

---

## 腳本 1：檢查 Slow Log 設定

```bash
#!/bin/bash
# check_slow_log_config.sh
# 用法：./check_slow_log_config.sh <context> <namespace> <statefulset>
# 檢查所有 pods 的 slow query log 設定狀態

CONTEXT="$1"
NAMESPACE="$2"
STS_NAME="$3"

if [ -z "$STS_NAME" ]; then
  echo "Usage: $0 <context> <namespace> <statefulset>"
  exit 1
fi

# Pod 名稱從 StatefulSet replicas 數生成（避免 label 誤匹配 MaxScale 等非 DB pod）
REPLICAS=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get sts "${STS_NAME}" \
  -o jsonpath='{.spec.replicas}')
if [ -z "$REPLICAS" ] || [ "$REPLICAS" -eq 0 ]; then
  echo "Error: StatefulSet ${STS_NAME} not found or has 0 replicas"
  exit 1
fi
PODS=""
for i in $(seq 0 $((REPLICAS - 1))); do
  PODS="${PODS} ${STS_NAME}-${i}"
done

# 從容器環境變數取得 root 密碼
MYSQL_PWD=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
  exec "${STS_NAME}-0" -c mariadb -- printenv MARIADB_ROOT_PASSWORD)

# 從 CR status 取得 Primary pod 名稱
CURRENT_PRIMARY=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
  get mariadb "${STS_NAME}" -o jsonpath='{.status.currentPrimary}')

get_role() {
  if [ "$1" = "$CURRENT_PRIMARY" ]; then
    echo "Primary"
  else
    echo "Replica"
  fi
}

for POD in $PODS; do
  ROLE=$(get_role "$POD")
  echo "=== ${POD} (${ROLE}) ==="

  # 查詢 slow log 相關變數
  kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
    mariadb -u root -p"${MYSQL_PWD}" -N -e "
      SHOW VARIABLES WHERE Variable_name IN (
        'slow_query_log',
        'slow_query_log_file',
        'long_query_time',
        'log_queries_not_using_indexes',
        'log_output'
      )
    " 2>/dev/null | while IFS=$'\t' read -r VAR_NAME VAR_VALUE; do
      printf "  %-35s %s\n" "${VAR_NAME}:" "${VAR_VALUE}"
    done

  # 取得 log 檔路徑（可能是相對路徑，需要加上 datadir）
  LOG_PATH=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
    mariadb -u root -p"${MYSQL_PWD}" -N -e \
    "SELECT IF(LEFT(@@slow_query_log_file,1)='/', @@slow_query_log_file, CONCAT(@@datadir, @@slow_query_log_file))" 2>/dev/null)

  if [ -n "$LOG_PATH" ]; then
    FILE_INFO=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
      sh -c "ls -lh '${LOG_PATH}' 2>/dev/null | awk '{print \$5}'" 2>/dev/null)
    LINE_COUNT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
      sh -c "wc -l < '${LOG_PATH}' 2>/dev/null" 2>/dev/null)

    if [ -n "$FILE_INFO" ]; then
      printf "  %-35s %s (%s lines)\n" "Log file:" "${FILE_INFO}" "${LINE_COUNT:-0}"
    else
      printf "  %-35s %s\n" "Log file:" "(not found or empty)"
    fi
  fi

  echo ""
done
```

### 輸出範例

```
=== mariadb-0 (Primary) ===
  slow_query_log:                     ON
  slow_query_log_file:                mariadb-0-slow.log
  long_query_time:                    1.000000
  log_queries_not_using_indexes:      ON
  log_output:                         FILE
  Log file:                           15M (12345 lines)

=== mariadb-1 (Replica) ===
  slow_query_log:                     ON
  slow_query_log_file:                mariadb-1-slow.log
  long_query_time:                    1.000000
  log_queries_not_using_indexes:      ON
  log_output:                         FILE
  Log file:                           20M (16789 lines)
```

---

## 腳本 2：查看 Slow Queries

```bash
#!/bin/bash
# show_slow_queries.sh
# 用法：./show_slow_queries.sh <context> <namespace> <statefulset> [options]
# 選項：
#   --pod <pod>     指定 pod（預設：查所有 pods）
#   --days <N>      查詢最近 N 天（1/3/7/14，預設：1）
#   --lines <N>     取最後 N 行（與 --days 互斥，優先級較高）
#   --top <N>       依 Query_time 排序，只顯示最慢的 N 筆（預設：顯示全部）

CONTEXT="$1"
NAMESPACE="$2"
STS_NAME="$3"
shift 3 2>/dev/null

if [ -z "$STS_NAME" ]; then
  echo "Usage: $0 <context> <namespace> <statefulset> [options]"
  echo "Options:"
  echo "  --pod <pod>     Target specific pod (default: all pods)"
  echo "  --days <N>      Last N days (1/3/7/14, default: 1)"
  echo "  --lines <N>     Last N lines (overrides --days)"
  echo "  --top <N>       Show only top N slowest queries"
  exit 1
fi

# 解析 options
TARGET_POD=""
DAYS=1
LINES=""
TOP=""

while [ $# -gt 0 ]; do
  case "$1" in
    --pod)   TARGET_POD="$2"; shift 2 ;;
    --days)  DAYS="$2"; shift 2 ;;
    --lines) LINES="$2"; shift 2 ;;
    --top)   TOP="$2"; shift 2 ;;
    *)       echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Pod 名稱從 StatefulSet replicas 數生成（避免 label 誤匹配 MaxScale 等非 DB pod）
REPLICAS=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get sts "${STS_NAME}" \
  -o jsonpath='{.spec.replicas}')
if [ -z "$REPLICAS" ] || [ "$REPLICAS" -eq 0 ]; then
  echo "Error: StatefulSet ${STS_NAME} not found or has 0 replicas"
  exit 1
fi

if [ -n "$TARGET_POD" ]; then
  PODS="$TARGET_POD"
else
  PODS=""
  for i in $(seq 0 $((REPLICAS - 1))); do
    PODS="${PODS} ${STS_NAME}-${i}"
  done
fi

# 從容器環境變數取得 root 密碼
MYSQL_PWD=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
  exec "${STS_NAME}-0" -c mariadb -- printenv MARIADB_ROOT_PASSWORD)

# 從 CR status 取得 Primary pod 名稱
CURRENT_PRIMARY=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
  get mariadb "${STS_NAME}" -o jsonpath='{.status.currentPrimary}')

get_role() {
  if [ "$1" = "$CURRENT_PRIMARY" ]; then
    echo "Primary"
  else
    echo "Replica"
  fi
}

# 計算時間範圍（用於 --days 模式）
if [ -z "$LINES" ]; then
  START_TIME=$(date -u -v-"${DAYS}"d '+%y%m%d %H:%M:%S' 2>/dev/null || \
               date -u -d "${DAYS} days ago" '+%y%m%d %H:%M:%S' 2>/dev/null)
  END_TIME=$(date -u '+%y%m%d %H:%M:%S')
fi

# awk 腳本：解析 MariaDB slow query log（POSIX 相容，不依賴 gawk）
# 注意：此 awk 跑在本地（pipe 的右側），不在容器內
AWK_PARSE='
BEGIN {
  entry_count = 0
  in_entry = 0
  current_time = ""
  current_query_time = ""
  current_lock_time = ""
  current_rows_exam = ""
  current_rows_sent = ""
  current_schema = ""
  current_user = ""
  current_sql = ""
  query_time_num = 0
}

# Time 行：新記錄開始
/^# Time: / {
  if (in_entry && current_sql != "") {
    print_entry()
  }
  in_entry = 1
  # "# Time: 260210  6:52:22" → 取 "260210 06:52:22"（正規化）
  s = substr($0, 9)
  gsub(/  +/, " ", s)
  # 正規化小時：MariaDB 可能輸出 1 位數小時（"6:52:22"），補零為 "06:52:22"
  sp = index(s, " ")
  if (sp > 0) {
    time_part = substr(s, sp + 1)
    if (length(time_part) == 7) {
      s = substr(s, 1, sp) "0" time_part
    }
  }
  current_time = s
  current_sql = ""
  current_schema = ""
  current_user = ""
  next
}

# User@Host 行：取 user 名稱
# 格式："# User@Host: root[root] @ localhost []"
/^# User@Host: / {
  s = $0
  sub(/^# User@Host: /, "", s)
  # user 是第一個 [ 之前的部分
  idx = index(s, "[")
  if (idx > 1) {
    current_user = substr(s, 1, idx - 1)
  } else {
    current_user = s
  }
  # 去除尾端空白
  gsub(/ +$/, "", current_user)
  next
}

# Thread_id 行：取 Schema
# 格式："# Thread_id: 123  Schema: myapp  QC_hit: No"
/^# Thread_id: / {
  current_schema = ""
  n = split($0, parts, "  ")
  for (i = 1; i <= n; i++) {
    if (index(parts[i], "Schema:") > 0) {
      sub(/.*Schema: */, "", parts[i])
      # 去除後面的欄位（QC_hit 等）
      sub(/ .*/, "", parts[i])
      current_schema = parts[i]
    }
  }
  next
}

# Query_time 行：取各數值
# 格式："# Query_time: 2.345678  Lock_time: 0.000123  Rows_sent: 1  Rows_examined: 50000"
/^# Query_time: / {
  n = split($0, parts, /  +/)
  for (i = 1; i <= n; i++) {
    if (index(parts[i], "Query_time:") > 0) {
      sub(/.*Query_time: */, "", parts[i])
      current_query_time = parts[i]
      query_time_num = parts[i] + 0
    } else if (index(parts[i], "Lock_time:") > 0) {
      sub(/.*Lock_time: */, "", parts[i])
      current_lock_time = parts[i]
    } else if (index(parts[i], "Rows_sent:") > 0) {
      sub(/.*Rows_sent: */, "", parts[i])
      current_rows_sent = parts[i]
    } else if (index(parts[i], "Rows_examined:") > 0) {
      sub(/.*Rows_examined: */, "", parts[i])
      current_rows_exam = parts[i]
    }
  }
  next
}

# 跳過 SET timestamp 行
/^SET timestamp=/ { next }
# 跳過 use 行
/^use / { next }
# 跳過其他 # 開頭的行（Rows_affected, Bytes_sent 等 metadata）
/^#/ { next }
# 跳過空行
/^$/ { next }
# 跳過 log header 行
/^\/.*mariadbd,/ { next }
/^Tcp port:/ { next }
/^Time[[:space:]]+Id/ { next }

# SQL 行（可能多行）
{
  if (in_entry) {
    if (current_sql == "") {
      current_sql = $0
    } else {
      current_sql = current_sql " " $0
    }
    # 移除尾端分號
    if ($0 ~ /;[[:space:]]*$/) {
      sub(/;[[:space:]]*$/, "", current_sql)
    }
  }
}

function print_entry() {
  # --days 模式：時間範圍過濾
  if (start_filter != "" && current_time < start_filter) return
  if (end_filter != "" && current_time > end_filter) return

  entry_count++

  if (top_n > 0) {
    # --top 模式：收集所有記錄，最後排序
    times[entry_count] = current_time
    qtimes[entry_count] = current_query_time
    qtimes_num[entry_count] = query_time_num
    ltimes[entry_count] = current_lock_time
    rexam[entry_count] = current_rows_exam
    rsent[entry_count] = current_rows_sent
    schemas[entry_count] = current_schema
    users[entry_count] = current_user
    sqls[entry_count] = current_sql
  } else {
    # 預設模式：直接輸出
    printf "[%d] %s | Query_time: %ss | Lock: %ss | Rows exam/sent: %s/%s\n", \
      entry_count, fmt_time(current_time), \
      fmt_secs(current_query_time), fmt_secs(current_lock_time), \
      current_rows_exam, current_rows_sent
    printf "    Schema: %s | User: %s\n", \
      (current_schema != "" ? current_schema : "N/A"), \
      (current_user != "" ? current_user : "N/A")
    printf "    %s\n\n", current_sql
  }
}

function fmt_time(t) {
  # 從 "YYMMDD HH:MM:SS"（已正規化）轉成 "20YY-MM-DD HH:MM:SS"
  if (length(t) >= 15) {
    return "20" substr(t,1,2) "-" substr(t,3,2) "-" substr(t,5,2) " " substr(t,8)
  }
  return t
}

function fmt_secs(s) {
  return sprintf("%.3f", s + 0)
}

END {
  # 處理最後一筆
  if (in_entry && current_sql != "") {
    print_entry()
  }

  if (top_n > 0 && entry_count > 0) {
    # 排序（selection sort — 記錄數通常不多）
    for (i = 1; i <= entry_count; i++) order[i] = i
    for (i = 1; i <= entry_count - 1; i++) {
      max_idx = i
      for (j = i + 1; j <= entry_count; j++) {
        if (qtimes_num[order[j]] > qtimes_num[order[max_idx]]) max_idx = j
      }
      if (max_idx != i) {
        tmp = order[i]; order[i] = order[max_idx]; order[max_idx] = tmp
      }
    }

    show_n = (top_n < entry_count) ? top_n : entry_count
    printf "%-4s | %10s | %10s | %14s | %-10s | %s\n", \
      "Rank", "Query_time", "Lock_time", "Rows exam/sent", "Schema", "SQL (truncated)"
    for (i = 1; i <= show_n; i++) {
      idx = order[i]
      sql_trunc = substr(sqls[idx], 1, 60)
      if (length(sqls[idx]) > 60) sql_trunc = sql_trunc "..."
      printf "%4d | %9ss | %9ss | %8s/%-5s | %-10s | %s\n", \
        i, fmt_secs(qtimes[idx]), fmt_secs(ltimes[idx]), \
        rexam[idx], rsent[idx], \
        (schemas[idx] != "" ? schemas[idx] : "N/A"), \
        sql_trunc
    }
    printf "\n--- %d queries in period, showing top %d ---\n", entry_count, show_n
  } else if (top_n == 0) {
    if (entry_count > 0) {
      printf "--- %d queries found (showing all) ---\n", entry_count
    } else {
      printf "--- No slow queries found ---\n"
    }
  } else {
    printf "--- No slow queries found ---\n"
  }
}
'

for POD in $PODS; do
  ROLE=$(get_role "$POD")

  # 取得 log 檔完整路徑（slow_query_log_file 可能是相對路徑，需加上 datadir）
  LOG_FILE=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
    mariadb -u root -p"${MYSQL_PWD}" -N -e \
    "SELECT IF(LEFT(@@slow_query_log_file,1)='/', @@slow_query_log_file, CONCAT(@@datadir, @@slow_query_log_file))" 2>/dev/null)

  if [ -z "$LOG_FILE" ]; then
    echo "=== ${POD} (${ROLE}) === [slow log file not found]"
    echo ""
    continue
  fi

  # 取得檔案大小和行數
  FILE_SIZE=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
    sh -c "ls -lh '${LOG_FILE}' 2>/dev/null | awk '{print \$5}'" 2>/dev/null)
  LINE_COUNT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
    sh -c "wc -l < '${LOG_FILE}' 2>/dev/null" 2>/dev/null)

  if [ -z "$FILE_SIZE" ]; then
    echo "=== ${POD} (${ROLE}) === [log file empty or not found]"
    echo ""
    continue
  fi

  # 顯示 header
  if [ -n "$LINES" ]; then
    echo "=== ${POD} (${ROLE}) === [log: ${FILE_SIZE}, ${LINE_COUNT} lines, last ${LINES} lines]"
  elif [ -n "$TOP" ]; then
    echo "=== ${POD} (${ROLE}) === [log: ${FILE_SIZE}, ${LINE_COUNT} lines, last ${DAYS} days, showing top ${TOP}]"
  else
    echo "=== ${POD} (${ROLE}) === [log: ${FILE_SIZE}, ${LINE_COUNT} lines, last ${DAYS} days]"
  fi
  echo ""

  # 取出 log 內容並在本地解析
  # 關鍵：awk 在 pipe 本地端執行，容器只負責 cat/tail
  if [ -n "$LINES" ]; then
    # --lines 模式：用 tail 取最後 N 行
    kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
      tail -n "${LINES}" "${LOG_FILE}" 2>/dev/null \
    | awk -v start_filter="" -v end_filter="" -v top_n="${TOP:-0}" "$AWK_PARSE"
  else
    # --days 模式：streaming cat + 本地 awk 時間過濾
    kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
      cat "${LOG_FILE}" 2>/dev/null \
    | awk -v start_filter="${START_TIME}" -v end_filter="${END_TIME}" -v top_n="${TOP:-0}" "$AWK_PARSE"
  fi

  echo ""
done
```

### 輸出範例

**預設模式**（`--days 1`）：

```
=== mariadb-0 (Primary) === [log: 15M, 12345 lines, last 1 days]

[1] 2025-02-10 14:30:00 | Query_time: 2.346s | Lock: 0.000s | Rows exam/sent: 50000/1
    Schema: myapp | User: app_user
    SELECT * FROM users WHERE email = 'test@test.com'

[2] 2025-02-10 14:29:55 | Query_time: 1.234s | Lock: 0.001s | Rows exam/sent: 30000/10
    Schema: myapp | User: app_user
    SELECT * FROM orders WHERE created_at > '2025-01-01' AND status = 'pending'

--- 2 queries found (showing all) ---
```

**Top N 模式**（`--days 7 --top 5`）：

```
=== mariadb-0 (Primary) === [log: 15M, 12345 lines, last 7 days, showing top 5]

Rank | Query_time | Lock_time | Rows exam/sent | Schema     | SQL (truncated)
   1 |     5.234s |    0.012s |   200000/1     | myapp      | SELECT * FROM orders WHERE ...
   2 |     2.346s |    0.000s |    50000/1     | myapp      | SELECT * FROM users WHERE e...
   3 |     1.890s |    0.000s |    30000/50    | myapp      | SELECT o.*, u.name FROM ord...

--- 128 queries in period, showing top 5 ---
```

### 慢查詢 Log 格式（MariaDB）

腳本解析的原始 log 格式如下：

```
# Time: 250210 14:30:00
# User@Host: app_user[app_user] @ app-pod [10.0.1.5]
# Thread_id: 123  Schema: myapp  QC_hit: No
# Query_time: 2.345678  Lock_time: 0.000123  Rows_sent: 1  Rows_examined: 50000
# Rows_affected: 0  Bytes_sent: 65
SET timestamp=1739194200;
SELECT * FROM users WHERE email = 'test@test.com';
```

**注意**：`# Rows_affected:` 和 `# Bytes_sent:` 是 MariaDB 特有的額外行，腳本會自動跳過。

---

## 使用流程

```
Step 1: ./check_slow_log_config.sh prod mariadb mariadb
        └── 確認 slow log 已啟用、門檻值、檔案大小

Step 2: ./show_slow_queries.sh prod mariadb mariadb --days 1
        └── 查看過去 1 天的慢查詢

Step 3: ./show_slow_queries.sh prod mariadb mariadb --days 7 --top 10
        └── 過去 7 天最慢的 10 筆

Step 4: 用現有腳本繼續調查
        ├── ./get_sql_by_digest.sh ... <digest>   ← 查其他 pod 上的同一 SQL
        └── ./analyze_sql.sh ... "<SQL>"           ← EXPLAIN 分析
```

---

## Rundeck Job 設定

### Job 1: Check Slow Log Config

```yaml
name: Check Slow Log Config
description: 檢查 slow query log 設定狀態
options:
  - name: CONTEXT
    description: Kubernetes context
    required: true
  - name: NAMESPACE
    description: MariaDB namespace
    required: true
    default: mariadb
  - name: STATEFULSET
    description: StatefulSet name
    required: true
    default: mariadb
sequence:
  commands:
    - script: |
        /path/to/check_slow_log_config.sh \
          "@option.CONTEXT@" \
          "@option.NAMESPACE@" \
          "@option.STATEFULSET@"
```

### Job 2: Show Slow Queries

```yaml
name: Show Slow Queries
description: 從 slow query log 取出慢查詢（本地解析，最小化 DB 影響）
options:
  - name: CONTEXT
    description: Kubernetes context
    required: true
  - name: NAMESPACE
    description: MariaDB namespace
    required: true
    default: mariadb
  - name: STATEFULSET
    description: StatefulSet name
    required: true
    default: mariadb
  - name: DAYS
    description: 查詢最近 N 天
    values: ["1", "3", "7", "14"]
    default: "1"
  - name: TOP
    description: 只顯示最慢的 N 筆（空白 = 全部）
    required: false
sequence:
  commands:
    - script: |
        OPTS=""
        if [ -n "@option.DAYS@" ]; then OPTS="$OPTS --days @option.DAYS@"; fi
        if [ -n "@option.TOP@" ]; then OPTS="$OPTS --top @option.TOP@"; fi
        /path/to/show_slow_queries.sh \
          "@option.CONTEXT@" \
          "@option.NAMESPACE@" \
          "@option.STATEFULSET@" \
          $OPTS
```
