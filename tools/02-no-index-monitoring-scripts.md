# No-Index Query 監控腳本

## 腳本 1：用 Digest 查留存的 SQL

```bash
#!/bin/bash
# get_sql_by_digest.sh
# 用法：./get_sql_by_digest.sh <context> <namespace> <statefulset> <digest> [--verbose]

CONTEXT="$1"
NAMESPACE="$2"
STS_NAME="$3"
DIGEST="$4"
VERBOSE="${5:-}"

if [ -z "$DIGEST" ]; then
  echo "Usage: $0 <context> <namespace> <statefulset> <digest> [--verbose]"
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

echo "Digest: ${DIGEST}"
echo "SQL_TEXT = 資料庫留存的 SQL；不保證完整。腳本不裁切 SQL，也不自行加上 ...。"
echo "capture_status: possibly_truncated = 達到上限；below_limit = 未達上限（不保證完整）；unknown = 無法判定。"
echo ""

FOUND=0

for POD in $PODS; do
  # 每個 pod 的設定可能不同。舊版／查詢失敗時使用 NULL，不假設預設值。
  if ! SQL_TEXT_LIMIT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
    mariadb -u root -p"${MYSQL_PWD}" -N -B -e "
      SELECT VARIABLE_VALUE
      FROM information_schema.GLOBAL_VARIABLES
      WHERE VARIABLE_NAME = 'PERFORMANCE_SCHEMA_MAX_SQL_TEXT_LENGTH'
    "); then
    SQL_TEXT_LIMIT=NULL
  fi
  case "$SQL_TEXT_LIMIT" in
    ''|*[!0-9]*) SQL_TEXT_LIMIT=NULL ;;
  esac

  # 與 SQL_TEXT 在同一次 SELECT 計算，避免 history 淘汰造成狀態與 SQL 不一致。
  CAPTURE_STATUS="CASE
    WHEN SQL_TEXT IS NULL OR ${SQL_TEXT_LIMIT} IS NULL THEN 'unknown'
    WHEN OCTET_LENGTH(SQL_TEXT) >= ${SQL_TEXT_LIMIT} THEN 'possibly_truncated'
    ELSE 'below_limit'
  END"

  if [ "$VERBOSE" = "--verbose" ]; then
    echo "----------------------------------------"
    echo "Pod: ${POD}"

    ROLE=$(get_role "$POD")
    echo "Role: ${ROLE}"

    # 先查 history（每個 thread 最近 N 筆，最快消失）
    RESULT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
      mariadb -u root -p"${MYSQL_PWD}" -e "
        SELECT IFNULL(CURRENT_SCHEMA, 'N/A') AS schema_name,
               SQL_TEXT AS sql_text,
               OCTET_LENGTH(SQL_TEXT) AS sql_text_bytes,
               ${SQL_TEXT_LIMIT} AS sql_text_limit_bytes,
               ${CAPTURE_STATUS} AS capture_status,
               ROWS_EXAMINED AS rows_examined,
               ROWS_SENT AS rows_sent
        FROM performance_schema.events_statements_history
        WHERE DIGEST = '${DIGEST}'
        ORDER BY EVENT_ID DESC
        LIMIT 2
      \G")

    if [ -n "$RESULT" ]; then
      echo "[Found in history]"
      echo "$RESULT"
      FOUND=1
    else
      # fallback: 查 history_long（全域保留，筆數多但也會被淘汰）
      RESULT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
        mariadb -u root -p"${MYSQL_PWD}" -e "
          SELECT IFNULL(CURRENT_SCHEMA, 'N/A') AS schema_name,
                 SQL_TEXT AS sql_text,
                 OCTET_LENGTH(SQL_TEXT) AS sql_text_bytes,
                 ${SQL_TEXT_LIMIT} AS sql_text_limit_bytes,
                 ${CAPTURE_STATUS} AS capture_status,
                 ROWS_EXAMINED AS rows_examined,
                 ROWS_SENT AS rows_sent
          FROM performance_schema.events_statements_history_long
          WHERE DIGEST = '${DIGEST}'
          ORDER BY EVENT_ID DESC
          LIMIT 2
        \G")

      if [ -n "$RESULT" ]; then
        echo "[Found in history_long]"
        echo "$RESULT"
        FOUND=1
      else
        echo "[Not found in history]"
      fi
    fi
    echo ""
  else
    RESULT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
      mariadb -u root -p"${MYSQL_PWD}" -N -e "
        SELECT CONCAT('[SQL_TEXT; bytes=', IFNULL(OCTET_LENGTH(SQL_TEXT), 'unknown'),
                      '; limit_bytes=', IFNULL(${SQL_TEXT_LIMIT}, 'unknown'),
                      '; capture_status=', ${CAPTURE_STATUS}, '] ', IFNULL(SQL_TEXT, '<NULL>'))
        FROM performance_schema.events_statements_history
        WHERE DIGEST = '${DIGEST}'
        LIMIT 1
      ")

    # fallback: 查 history_long
    if [ -z "$RESULT" ]; then
      RESULT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
        mariadb -u root -p"${MYSQL_PWD}" -N -e "
          SELECT CONCAT('[SQL_TEXT; bytes=', IFNULL(OCTET_LENGTH(SQL_TEXT), 'unknown'),
                      '; limit_bytes=', IFNULL(${SQL_TEXT_LIMIT}, 'unknown'),
                      '; capture_status=', ${CAPTURE_STATUS}, '] ', IFNULL(SQL_TEXT, '<NULL>'))
          FROM performance_schema.events_statements_history_long
          WHERE DIGEST = '${DIGEST}'
          LIMIT 1
        ")
    fi

    if [ -n "$RESULT" ]; then
      ROLE=$(get_role "$POD")
      echo "[${POD}] (${ROLE}) ${RESULT}"
      FOUND=1
    fi
  fi
done

if [ "$FOUND" -eq 0 ] || [ "$VERBOSE" = "--verbose" ]; then
  echo ""
  echo "=== DIGEST_TEXT：正規化模板（from summary） ==="
  echo "? 是參數佔位；... 可能是清單正規化或長度限制，不能只靠點點判斷截斷。"
  echo "模板不能還原原始參數或已截斷內容。"
  for POD in $PODS; do
    ROLE=$(get_role "$POD")
    SUMMARY=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
      mariadb -u root -p"${MYSQL_PWD}" -e "
        SELECT IFNULL(SCHEMA_NAME, 'N/A') AS schema_name,
               DIGEST_TEXT AS pattern,
               SUM_NO_INDEX_USED AS no_index_count,
               COUNT_STAR AS total_exec
        FROM performance_schema.events_statements_summary_by_digest
        WHERE DIGEST = '${DIGEST}'
      \G")

    if [ -n "$SUMMARY" ]; then
      echo "[${POD}] (${ROLE})"
      echo "$SUMMARY"
      echo ""
      FOUND=1
    fi
  done

  if [ "$FOUND" -eq 0 ]; then
    echo "Digest ${DIGEST} not found on any pod."
    echo "Debug: 確認 digest 值正確，或該 SQL 已從 performance_schema 中被淘汰"
  fi
fi
```

### 判讀長 SQL

- `SQL_TEXT` 是 history／history_long 留存的內容；腳本原樣輸出，不保證資料庫收錄時未截斷。
- `sql_text_bytes`／`bytes` 是資料庫內 `SQL_TEXT` 的位元組數（不是終端輸出字元數）；`sql_text_limit_bytes`／`limit_bytes` 是該 pod 的 `performance_schema_max_sql_text_length`。
- `possibly_truncated` 表示長度達到上限，可能已截斷；剛好等於上限的完整 SQL 也會提示。
- `below_limit` 只表示未達目前上限，不是完整性保證；`unknown` 表示 SQL 為 NULL 或設定無法取得。腳本不猜預設值，也不修改資料庫設定。
- `DIGEST_TEXT` 是正規化模板。`?` 與 `...` 不足以證明原始 SQL 是否完整；`--verbose` 也無法補回資料庫未保留的內容。

### 輸出範例

**精簡版（預設）**：
```
Digest: abc123def456...

[mariadb-0] (Primary) [SQL_TEXT; bytes=52; limit_bytes=1024; capture_status=below_limit] SELECT * FROM users WHERE email = 'test@example.com'
[mariadb-2] (Replica) [SQL_TEXT; bytes=47; limit_bytes=1024; capture_status=below_limit] SELECT * FROM users WHERE email = 'foo@bar.com'

```

**完整版（--verbose）**：
```
Digest: abc123def456...

----------------------------------------
Pod: mariadb-0
Role: Primary
[Found in history]
*************************** 1. row ***************************
  schema_name: myapp
     sql_text: SELECT * FROM users WHERE email = 'test@test.com'
sql_text_bytes: 49
sql_text_limit_bytes: 1024
capture_status: below_limit
rows_examined: 50000
    rows_sent: 1

----------------------------------------
Pod: mariadb-1
Role: Replica
[Found in history_long]
*************************** 1. row ***************************
  schema_name: myapp
     sql_text: SELECT * FROM users WHERE email = 'another@test.com'
sql_text_bytes: 52
sql_text_limit_bytes: 1024
capture_status: below_limit
rows_examined: 50000
    rows_sent: 1

----------------------------------------
Pod: mariadb-2
Role: Replica
[Not found in history]

=== DIGEST_TEXT：正規化模板（from summary） ===
[mariadb-0] (Primary)
*************************** 1. row ***************************
   schema_name: myapp
       pattern: SELECT * FROM `users` WHERE `email` = ?
no_index_count: 5000
    total_exec: 5000

[mariadb-1] (Replica)
*************************** 1. row ***************************
   schema_name: myapp
       pattern: SELECT * FROM `users` WHERE `email` = ?
no_index_count: 50000
    total_exec: 50000

[mariadb-2] (Replica)
*************************** 1. row ***************************
   schema_name: myapp
       pattern: SELECT * FROM `users` WHERE `email` = ?
no_index_count: 50000
    total_exec: 50000
```

---

## 腳本 2：分析 SQL（增強版）

```bash
#!/bin/bash
# analyze_sql.sh (增強版)
# 用法：./analyze_sql.sh <context> <namespace> <statefulset> "<SQL>" [--analyze]
# 自動找到 Primary pod 執行 EXPLAIN
# --analyze: 執行 EXPLAIN ANALYZE 顯示實際執行時間

CONTEXT="$1"
NAMESPACE="$2"
STS_NAME="$3"
SQL="$4"
ANALYZE_FLAG="$5"

if [ -z "$SQL" ]; then
  echo "Usage: $0 <context> <namespace> <statefulset> \"<SQL>\" [--analyze]"
  echo ""
  echo "Options:"
  echo "  --analyze    執行 EXPLAIN ANALYZE（會實際執行 SQL）"
  exit 1
fi

# Pod 名稱從 StatefulSet replicas 數生成（避免 label 誤匹配 MaxScale 等非 DB pod）
REPLICAS=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get sts "${STS_NAME}" \
  -o jsonpath='{.spec.replicas}')
if [ -z "$REPLICAS" ] || [ "$REPLICAS" -eq 0 ]; then
  echo "Error: StatefulSet ${STS_NAME} not found or has 0 replicas"
  exit 1
fi

# 從容器環境變數取得 root 密碼
MYSQL_PWD=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
  exec "${STS_NAME}-0" -c mariadb -- printenv MARIADB_ROOT_PASSWORD)

# 從 CR status 取得 Primary pod 名稱
PRIMARY_POD=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
  get mariadb "${STS_NAME}" -o jsonpath='{.status.currentPrimary}')

if [ -z "$PRIMARY_POD" ]; then
  echo "Error: No Primary pod found in CR status (.status.currentPrimary)"
  exit 1
fi

# Helper: 執行 SQL（無格式）
run_sql() {
  kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${PRIMARY_POD}" -c mariadb -- \
    mariadb -u root -p"${MYSQL_PWD}" -N -e "$1" 2>/dev/null
}

# Helper: 執行 SQL（表格格式）
run_sql_pretty() {
  kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${PRIMARY_POD}" -c mariadb -- \
    mariadb -u root -p"${MYSQL_PWD}" -e "$1" 2>/dev/null
}

echo "=== Target ==="
echo "StatefulSet: ${STS_NAME}"
echo "Primary Pod: ${PRIMARY_POD}"
echo ""

echo "=== SQL ==="
echo "$SQL"
echo ""

# ========== 新增：Table Structure ==========
echo "=== Table Structure ==="

# 抽取 table 名稱 (FROM, JOIN, UPDATE, INTO)
TABLES=$(echo "$SQL" | grep -oiE '(FROM|JOIN|UPDATE|INTO)\s+`?[a-zA-Z_][a-zA-Z0-9_]*`?' | \
         sed -E 's/(FROM|JOIN|UPDATE|INTO)\s+`?//gi' | tr -d '`' | sort -u)

for TABLE in $TABLES; do
  echo "--- $TABLE ---"

  # 顯示 CREATE TABLE（含 indexes）
  run_sql_pretty "SHOW CREATE TABLE $TABLE\G" | grep -v "^\*\*\*"

  # 顯示資料量
  ROW_COUNT=$(run_sql "SELECT COUNT(*) FROM $TABLE")
  echo "Rows: $ROW_COUNT"

  if [ -n "$ROW_COUNT" ] && [ "$ROW_COUNT" -lt 1000 ]; then
    echo "Note: 小表，Full Scan 可能是正常的"
  fi
  echo ""
done

# ========== EXPLAIN ==========
echo "=== EXPLAIN ==="
run_sql_pretty "EXPLAIN ${SQL}"
echo ""

# ========== EXPLAIN ANALYZE（可選）==========
if [ "$ANALYZE_FLAG" = "--analyze" ]; then
  echo "=== EXPLAIN ANALYZE ==="
  echo "(實際執行 SQL，顯示真實時間和行數)"
  run_sql_pretty "EXPLAIN ANALYZE ${SQL}"
  echo ""
fi

# ========== 解讀 ==========
echo "=== 解讀 ==="
run_sql_pretty "
  SELECT
    CASE
      WHEN type = 'ALL' THEN '!! Full Table Scan'
      WHEN type = 'index' THEN '! Full Index Scan'
      WHEN type IN ('ref', 'eq_ref', 'range', 'const') THEN 'OK - Using Index'
      ELSE CONCAT('type=', type)
    END AS diagnosis,
    IFNULL(possible_keys, 'none') AS possible_indexes,
    IFNULL(key, 'none') AS actual_index,
    rows AS estimated_rows
  FROM (EXPLAIN ${SQL}) AS e
"
echo ""

# ========== 新增：Index Suggestion ==========
echo "=== Index Suggestion ==="

# 抽取 WHERE 和 AND 條件的欄位
WHERE_COLS=$(echo "$SQL" | grep -oiE '(WHERE|AND|OR)\s+`?[a-zA-Z_][a-zA-Z0-9_]*`?\s*(=|>|<|LIKE|IN)' | \
             sed -E 's/(WHERE|AND|OR)\s+`?//gi' | sed -E 's/\s*(=|>|<|LIKE|IN).*//i' | tr -d '`' | sort -u)

# 抽取 JOIN ON 條件的欄位
JOIN_COLS=$(echo "$SQL" | grep -oiE 'ON\s+`?[a-zA-Z_][a-zA-Z0-9_.]*`?\s*=' | \
            sed -E 's/ON\s+`?//gi' | sed 's/\s*=//g' | tr -d '`' | sort -u)

HAS_SUGGESTION=0

if [ -n "$WHERE_COLS" ]; then
  echo "WHERE 條件欄位："
  for COL in $WHERE_COLS; do
    echo "  - $COL"
  done
  echo ""
  echo "建議 Index（如果還沒有）："
  for TABLE in $TABLES; do
    for COL in $WHERE_COLS; do
      echo "  ALTER TABLE $TABLE ADD INDEX idx_${COL} (${COL});"
    done
  done
  HAS_SUGGESTION=1
fi

if [ -n "$JOIN_COLS" ]; then
  echo ""
  echo "JOIN 條件欄位："
  for COL in $JOIN_COLS; do
    echo "  - $COL"
  done
  HAS_SUGGESTION=1
fi

if [ "$HAS_SUGGESTION" -eq 0 ]; then
  echo "無法從 SQL 自動抽取建議，請手動檢查 WHERE/JOIN 條件"
fi

echo ""

# ========== 檢查清單 ==========
echo "=== 檢查清單 ==="
echo "[ ] type=ALL? 需要加 Index"
echo "[ ] possible_keys 有值但 key 是 NULL? Optimizer 選擇不用（可能正常）"
echo "[ ] WHERE 條件有 Function? 例如 YEAR(date)"
echo "[ ] 有隱式型別轉換? VARCHAR vs INT"
echo "[ ] LIKE 開頭是 %?"
echo "[ ] 統計資訊過時? 試試 ANALYZE TABLE"
```

### 輸出範例

**沒用 Index（有問題）**：
```
=== Target ===
StatefulSet: mariadb
Primary Pod: mariadb-0

=== SQL ===
SELECT * FROM users WHERE email = 'test@test.com'

=== Table Structure ===
--- users ---
       Table: users
Create Table: CREATE TABLE `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `email` varchar(255) DEFAULT NULL,
  `name` varchar(100) DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
Rows: 100000

=== EXPLAIN ===
+----+-------------+-------+------+---------------+------+---------+------+--------+-------------+
| id | select_type | table | type | possible_keys | key  | key_len | ref  | rows   | Extra       |
+----+-------------+-------+------+---------------+------+---------+------+--------+-------------+
|  1 | SIMPLE      | users | ALL  | NULL          | NULL | NULL    | NULL | 100000 | Using where |
+----+-------------+-------+------+---------------+------+---------+------+--------+-------------+

=== 解讀 ===
+----------------------+------------------+--------------+----------------+
| diagnosis            | possible_indexes | actual_index | estimated_rows |
+----------------------+------------------+--------------+----------------+
| !! Full Table Scan   | none             | none         | 100000         |
+----------------------+------------------+--------------+----------------+

=== Index Suggestion ===
WHERE 條件欄位：
  - email

建議 Index（如果還沒有）：
  ALTER TABLE users ADD INDEX idx_email (email);

=== 檢查清單 ===
[ ] type=ALL? 需要加 Index
[ ] possible_keys 有值但 key 是 NULL? Optimizer 選擇不用（可能正常）
[ ] WHERE 條件有 Function? 例如 YEAR(date)
[ ] 有隱式型別轉換? VARCHAR vs INT
[ ] LIKE 開頭是 %?
[ ] 統計資訊過時? 試試 ANALYZE TABLE
```

**有用 Index（正常）**：
```
=== Target ===
StatefulSet: mariadb
Primary Pod: mariadb-1

=== SQL ===
SELECT * FROM users WHERE email = 'test@test.com'

=== Table Structure ===
--- users ---
       Table: users
Create Table: CREATE TABLE `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `email` varchar(255) DEFAULT NULL,
  `name` varchar(100) DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_email` (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
Rows: 100000

=== EXPLAIN ===
+----+-------------+-------+------+---------------+-----------+---------+-------+------+-------+
| id | select_type | table | type | possible_keys | key       | key_len | ref   | rows | Extra |
+----+-------------+-------+------+---------------+-----------+---------+-------+------+-------+
|  1 | SIMPLE      | users | ref  | idx_email     | idx_email | 767     | const |    1 |       |
+----+-------------+-------+------+---------------+-----------+---------+-------+------+-------+

=== 解讀 ===
+------------------+------------------+--------------+----------------+
| diagnosis        | possible_indexes | actual_index | estimated_rows |
+------------------+------------------+--------------+----------------+
| OK - Using Index | idx_email        | idx_email    | 1              |
+------------------+------------------+--------------+----------------+

=== Index Suggestion ===
WHERE 條件欄位：
  - email

建議 Index（如果還沒有）：
  ALTER TABLE users ADD INDEX idx_email (email);

=== 檢查清單 ===
[ ] type=ALL? 需要加 Index
...
```

**JOIN 查詢範例**：
```
=== Target ===
StatefulSet: mariadb
Primary Pod: mariadb-0

=== SQL ===
SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id WHERE o.status = 'pending'

=== Table Structure ===
--- users ---
       Table: users
Create Table: CREATE TABLE `users` (...)
Rows: 100000

--- orders ---
       Table: orders
Create Table: CREATE TABLE `orders` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `user_id` int(11) DEFAULT NULL,
  `status` varchar(20) DEFAULT NULL,
  `total` decimal(10,2) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
Rows: 500000

=== EXPLAIN ===
...

=== Index Suggestion ===
WHERE 條件欄位：
  - status

建議 Index（如果還沒有）：
  ALTER TABLE users ADD INDEX idx_status (status);
  ALTER TABLE orders ADD INDEX idx_status (status);

JOIN 條件欄位：
  - u.id
  - o.user_id

=== 檢查清單 ===
...
```

**小表範例（Full Scan 正常）**：
```
=== Table Structure ===
--- config ---
       Table: config
Create Table: CREATE TABLE `config` (...)
Rows: 50
Note: 小表，Full Scan 可能是正常的

=== 解讀 ===
+----------------------+------------------+--------------+----------------+
| diagnosis            | possible_indexes | actual_index | estimated_rows |
+----------------------+------------------+--------------+----------------+
| !! Full Table Scan   | none             | none         | 50             |
+----------------------+------------------+--------------+----------------+
```

**EXPLAIN ANALYZE 範例（--analyze flag）**：
```
=== EXPLAIN ANALYZE ===
(實際執行 SQL，顯示真實時間和行數)
+----+-------------+-------+------+---------------+-----------+---------+-------+------+----------+------------+
| id | select_type | table | type | possible_keys | key       | key_len | ref   | rows | r_rows   | r_time_ms  |
+----+-------------+-------+------+---------------+-----------+---------+-------+------+----------+------------+
|  1 | SIMPLE      | users | ref  | idx_email     | idx_email | 767     | const |    1 |     1.00 |       0.05 |
+----+-------------+-------+------+---------------+-----------+---------+-------+------+----------+------------+
```

---

## 使用流程

```
Step 1: 從 Grafana 拿到 digest
        │
        ▼
Step 2: ./get_sql_by_digest.sh prod mariadb mariadb <digest>
        └── 得到留存 SQL 與可能截斷狀態
        │
        ▼
Step 3: ./analyze_sql.sh prod mariadb mariadb "<SQL>"
        └── 自動找 Primary pod 執行 EXPLAIN
        │
        ├── type=ALL, possible_keys=none → 需要加 index
        ├── type=ALL, possible_keys 有值 → Optimizer 選擇不用，可能正常
        └── type=ref/range → 已經有用 index
```

---

## Rundeck Job 設定

### Job 1: Get SQL by Digest

```yaml
name: Get SQL by Digest
description: 用 digest 查詢留存 SQL 與可能截斷狀態（查所有 pods）
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
  - name: DIGEST
    description: SQL digest from Prometheus
    required: true
  - name: VERBOSE
    description: Show detailed output
    values: ["", "--verbose"]
sequence:
  commands:
    - script: |
        /path/to/get_sql_by_digest.sh \
          "@option.CONTEXT@" \
          "@option.NAMESPACE@" \
          "@option.STATEFULSET@" \
          "@option.DIGEST@" \
          @option.VERBOSE@
```

### Job 2: Analyze SQL

```yaml
name: Analyze SQL
description: 分析 SQL 的執行計畫（自動選擇 Primary pod）
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
  - name: SQL
    description: SQL to analyze
    required: true
  - name: ANALYZE
    description: 執行 EXPLAIN ANALYZE（會實際執行 SQL）
    values: ["", "--analyze"]
sequence:
  commands:
    - script: |
        /path/to/analyze_sql.sh \
          "@option.CONTEXT@" \
          "@option.NAMESPACE@" \
          "@option.STATEFULSET@" \
          "@option.SQL@" \
          @option.ANALYZE@
```
