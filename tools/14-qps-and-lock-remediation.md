# QPS 監控、Row Lock Alert 改進與自動殺 Blocking Transaction

## 一、Queries vs Questions 監控

### 核心差異

MariaDB/MySQL 有兩個容易混淆的 status 變數：

| 變數 | 計算範圍 | Prometheus Metric |
|------|---------|-------------------|
| `Questions` | **僅計算 client 送來的語句** | `mysql_global_status_questions` |
| `Queries` | **全部語句**，包含 stored procedure、trigger、event 內部執行的 | `mysql_global_status_queries` |

### 範例

```sql
-- 假設有一個 stored procedure 包含 50 條 SQL：
CALL process_orders();

-- Questions += 1（只算 CALL 這條）
-- Queries  += 51（CALL + 50 條內部 SQL）
```

### Primary vs Replica 的差異

Replica 上 `Queries > Questions` 是**正常現象**，因為 replication SQL thread 重播 Primary 的寫入語句：

```
Primary:  Questions = 1000, Queries = 1000（純 client SQL）
Replica:  Questions = 200,  Queries = 1200
              │                  │
              │                  └─ 200 client SELECT + 1000 replication SQL
              └─ 200 client SELECT only
```

| 來源 | 計入 Questions | 計入 Queries |
|------|:---:|:---:|
| Client SQL（SELECT/INSERT/UPDATE...） | ✓ | ✓ |
| Replication SQL thread（Replica 重播 binlog） | ✗ | ✓ |
| Stored procedure 內部語句 | ✗ | ✓ |

**此環境不允許 stored procedure**，因此：
- **Primary 上** `Queries ≈ Questions`（差異幾乎為 0）
- **Replica 上** `Queries > Questions`（差值 = replication 重播的寫入量，正常行為）
- **監控重點放在 `Questions`**（client QPS），因為它更準確反映 client 負載

### 實用 PromQL

```promql
# Client QPS（每秒 client 送來的語句數）— 主要監控指標
rate(mysql_global_status_questions[5m])

# 總 QPS（每秒所有語句數，含 replication 重播）
rate(mysql_global_status_queries[5m])

# Replica 上的 replication 重播量（僅供觀察，不建議設 alert）
rate(mysql_global_status_queries[5m]) - rate(mysql_global_status_questions[5m])
```

### 關於 `rate(mysql_global_status_queries) > 0.25` 的討論

用戶提到的 alert rule `rate > 0.25`，可能有幾種解讀：

| 解讀 | 意義 | 問題 |
|------|------|------|
| `rate(queries[5m]) > 0.25` QPS | 每秒 0.25 個 query | 門檻極低，幾乎任何 DB 都會觸發 |
| `rate(queries[5m]) / rate(questions[5m]) > 0.25` 比例 | 內部 query 佔 25% 以上 | 合理，偵測 stored procedure 負擔 |
| `deriv(rate(queries[5m])) > 0.25` 加速度 | QPS 增長速率 | 偵測 QPS 暴增趨勢 |

**建議**：QPS alert 應該用相對基線，而非絕對值。不同環境的正常 QPS 差異極大。

### 為什麼 QPS Alert 無法通用

QPS alert 使用 `avg_over_time` 基線倍數，在間歇性負載（batch job）下會誤觸發：

```
Batch job 每天 9am 跑 1 小時（平時 QPS = 0）：
  1h avg（8:00-9:00）= 0 → job 啟動時 QPS 從 0 暴衝 → 任何倍數都觸發（誤報）
  1h avg（9:00-10:00）= 200 → job 結束 QPS 降到 0 → drop alert 觸發（誤報）
```

Prometheus 原生無法做「跟歷史同時段比較」的 anomaly detection。
**QPS alert 需要 app team 根據自己的流量模式設定門檻**。

### QPS Alert 模板（需要 app team 根據自己的流量模式設定門檻）

#### Template 1: 穩定流量型（OLTP、API backend）

適合 24/7 有持續流量的 app，用基線倍數偵測異常。

```yaml
# Adjust 1.5 multiplier and 1h window to match your traffic pattern
- alert: MariaDBQPSSpike
  expr: rate(mysql_global_status_questions[5m]) > 1.5 * avg_over_time(rate(mysql_global_status_questions[5m])[1h:1m])
  for: 2m
  labels:
    severity: info
    group_name: user
  annotations:
    summary: "MariaDB client QPS spike on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "Client QPS exceeds 1.5x the 1-hour average."
```

#### Template 2: 持續流量型（QPS 掉到 0 = 有問題）

適合 app 應該隨時有流量，QPS 驟降代表 app 連不上或上游掛了。

```yaml
# Only use if your app runs 24/7 with steady traffic
- alert: MariaDBQPSDrop
  expr: rate(mysql_global_status_questions[5m]) < 0.5 * avg_over_time(rate(mysql_global_status_questions[5m])[1h:1m]) and rate(mysql_global_status_questions[5m]) > 0
  for: 3m
  labels:
    severity: info
    group_name: user
  annotations:
    summary: "MariaDB client QPS drop on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "Client QPS dropped below 50% of baseline."
```

#### Template 3: 間歇/批次型（batch job、periodic task）

適合平時 QPS ≈ 0、只在特定時間有流量的 app，用絕對門檻。

```yaml
# Set threshold to your job's max expected QPS
- alert: MariaDBQPSHigh
  expr: rate(mysql_global_status_questions[5m]) > 500
  for: 5m
  labels:
    severity: info
    group_name: user
  annotations:
    summary: "MariaDB client QPS high on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "Client QPS exceeds expected threshold."
```

### 通用 DB 健康 Alerts（不依賴 app 流量模式）

以下 alert 監控的是 **QPS 造成的影響**，不是 QPS 本身，因此適用於所有 app。

**`threads_running`**：正在執行 SQL 的 thread 數量（不含 idle 連線）。
不管 app 的 QPS 模式如何，`threads_running` 過高代表 DB 被壓垮。

門檻參考（依 CPU cores）：

| DB 規格 | Cores | warning (> 2x) | critical (> 5x) |
|---------|:---:|:---:|:---:|
| Small | 2 | > 4 | > 10 |
| Medium | 4 | > 8 | > 20 |
| Large | 8 | > 16 | > 40 |

以下用保守通用值（兼顧所有規格）：

**`group_name` routing 規則**：
- `group_name: all` → platform + user 都收到
- `group_name: platform` → 只有 platform team 收到
- `group_name: user` → 只有 app team 收到

```yaml
# === Universal DB Health Alerts (no app-specific threshold needed) ===

# Threads running high — both platform and user should know
- alert: MariaDBThreadsRunningHigh
  expr: mysql_global_status_threads_running > 10
  for: 2m
  labels:
    severity: warning
    group_name: all
  annotations:
    summary: "MariaDB threads running > 10 on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} has {{ $value }} threads actively executing queries. DB may be overloaded. Check for slow queries or missing indexes."

# Threads running critical — platform infrastructure decision
- alert: MariaDBThreadsRunningCritical
  expr: mysql_global_status_threads_running > 30
  for: 1m
  labels:
    severity: warning
    group_name: platform
  annotations:
    summary: "MariaDB threads running > 30 on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} has {{ $value }} threads actively executing queries. Severe overload, may need resource scaling or rate limiting."

# Slow queries increasing — user app issue
- alert: MariaDBSlowQueriesSpike
  expr: rate(mysql_global_status_slow_queries[5m]) > 0.5
  for: 2m
  labels:
    severity: info
    group_name: user
  annotations:
    summary: "MariaDB slow queries increasing on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} is generating {{ $value | printf \"%.1f\" }} slow queries/sec. Check slow query log for missing indexes or lock contention."

# Container CPU throttling — platform resource limit issue
# sum by removes per-cpu-core duplicates, excludes node label to survive pod rescheduling
- alert: MariaDBCPUThrottlingHigh
  expr: sum by (namespace, pod, container) (rate(container_cpu_cfs_throttled_seconds_total{container="mariadb"}[5m])) > 0.25
  for: 5m
  labels:
    severity: warning
    group_name: platform
  annotations:
    summary: "MariaDB container CPU throttling on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} is being CPU throttled. Consider increasing CPU limits."
```

---

## 二、改進 Row Lock Alert Rules

### 為什麼 `innodb_row_lock_current_waits > 0` 太敏感

在任何有併發寫入的系統中，**短暫的 row lock wait 是完全正常的**：

```
Transaction A: UPDATE orders SET status='done' WHERE id=5;  ← 持有 X lock
Transaction B: UPDATE orders SET status='cancel' WHERE id=5; ← 等待 X lock（正常！）
                                                                 等 0.001 秒後 A commit
                                                                 B 立刻取得 lock ✓
```

`> 0` 會在這種正常情境下不斷觸發，造成 alert 疲勞。

### 什麼時候才是真正的問題

| 情境 | 特徵 | 嚴重度 |
|------|------|--------|
| 正常短暫 lock | `current_waits` 瞬間 > 0，幾秒內歸零 | 無需 alert |
| 持續性競爭 | `current_waits` 持續 > 3 超過 2 分鐘 | warning |
| Lock wait 暴增 | 5 分鐘內 lock wait 次數增加 > 10 次 | warning |
| 長時間等待 | 平均等待時間 > 5 秒 | critical |
| 大量 thread 被阻塞 | `current_waits` > 10 | critical |

### 改進的 Alert Rules

```yaml
- alert: MariaDBRowLockContention
  expr: mysql_global_status_innodb_row_lock_current_waits > 3
  for: 2m
  labels:
    severity: warning
  annotations:
    summary: "Sustained row lock contention on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} has {{ $value }} threads waiting for row locks for over 2 minutes. Run check_row_locks.sh to identify blocking SQL. See 11-row-lock-diagnosis.md."

- alert: MariaDBRowLockWaitSpike
  expr: increase(mysql_global_status_innodb_row_lock_waits[5m]) > 10
  for: 0m
  labels:
    severity: warning
  annotations:
    summary: "Row lock wait spike on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} had {{ $value | printf \"%.0f\" }} new row lock waits in the last 5 minutes. Possible batch job or hot-row contention."

- alert: MariaDBRowLockTimeHigh
  expr: increase(mysql_global_status_innodb_row_lock_time[5m]) / clamp_min(increase(mysql_global_status_innodb_row_lock_waits[5m]), 1) > 5000
  for: 1m
  labels:
    severity: critical
  annotations:
    summary: "Recent avg row lock wait > 5s on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} average row lock wait in the last 5 minutes is {{ $value }}ms. Transactions are being significantly delayed. Immediate investigation needed."

- alert: MariaDBRowLockSevere
  expr: mysql_global_status_innodb_row_lock_current_waits > 10
  for: 30s
  labels:
    severity: critical
  annotations:
    summary: "Severe row lock contention on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} has {{ $value }} threads waiting for row locks. This may cause cascading timeouts and app errors."
```

### 與 `11-row-lock-diagnosis.md` 的關係

```
Alert 觸發
  │
  ├─ MariaDBRowLockContention / MariaDBRowLockSevere
  │   → 執行 check_row_locks.sh（見 11-row-lock-diagnosis.md 第三節）
  │   → 找出 blocking SQL 和 waiting SQL
  │   → 手動決定是否 KILL
  │
  └─ 如果問題頻繁發生
      → 考慮啟用第三節的自動殺 blocking 腳本
```

---

## 三、自動殺 Blocking Transaction（Bash 腳本）

### 適用場景

- 已知某些長 transaction 會卡住其他 query（例如報表 job 忘記 commit）
- 希望在 blocking 超過一定時間後自動處理，減少人工介入
- **注意**：自動 KILL 是高風險操作，務必理解安全機制後再啟用

### kill_blocking_trx.sh

```bash
#!/bin/bash
# kill_blocking_trx.sh — 自動殺 blocking 超過指定時間的 transaction
#
# 用法：
#   bash kill_blocking_trx.sh
#
# 環境變數（可在 crontab 或 script 前設定）：
#   KUBE_CONTEXT   — kubectl context（預設 kind-mdb）
#   NAMESPACE      — namespace（預設 default）
#   POD_NAME       — MariaDB pod 名稱（預設 mariadb-chaos-0）
#   CONTAINER      — container 名稱（預設 mariadb）
#   MAX_BLOCK_SEC  — blocking 超過幾秒才殺（預設 300）

set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-mdb}"
NAMESPACE="${NAMESPACE:-default}"
POD_NAME="${POD_NAME:-mariadb-chaos-0}"
CONTAINER="${CONTAINER:-mariadb}"
MAX_BLOCK_SEC="${MAX_BLOCK_SEC:-300}"
MYSQL_PWD=$(kubectl --context="${KUBE_CONTEXT}" -n "${NAMESPACE}" \
  exec "${POD_NAME}" -c "${CONTAINER}" -- printenv MARIADB_ROOT_PASSWORD 2>/dev/null)

LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')] kill_blocking_trx:"

# --- Step 1: 找出 blocking 超過 MAX_BLOCK_SEC 的 thread ---
FIND_SQL="
SELECT DISTINCT b.trx_mysql_thread_id,
       TIMESTAMPDIFF(SECOND, b.trx_started, NOW()) AS blocking_sec,
       b.trx_query AS blocking_query
FROM information_schema.innodb_lock_waits lw
JOIN information_schema.innodb_trx b ON b.trx_id = lw.blocking_trx_id
JOIN information_schema.innodb_trx r ON r.trx_id = lw.requesting_trx_id
WHERE b.trx_mysql_thread_id > 10
  AND TIMESTAMPDIFF(SECOND, b.trx_started, NOW()) > ${MAX_BLOCK_SEC}
ORDER BY blocking_sec DESC;
"

RESULT=$(kubectl --context="${KUBE_CONTEXT}" -n "${NAMESPACE}" \
  exec "${POD_NAME}" -c "${CONTAINER}" -- \
  bash -c "MYSQL_PWD='${MYSQL_PWD}' mysql -u root -sN -e \"${FIND_SQL}\"" 2>/dev/null || true)

if [ -z "${RESULT}" ]; then
  echo "${LOG_PREFIX} No blocking transactions exceeding ${MAX_BLOCK_SEC}s"
  exit 0
fi

echo "${LOG_PREFIX} Found blocking transactions:"
echo "${RESULT}"

# --- Step 2: 逐一 KILL ---
echo "${RESULT}" | while IFS=$'\t' read -r THREAD_ID BLOCK_SEC BLOCK_QUERY; do
  # 安全檢查：跳過 thread_id < 10（系統/複製 thread）
  if [ "${THREAD_ID}" -le 10 ] 2>/dev/null; then
    echo "${LOG_PREFIX} SKIP thread ${THREAD_ID} (system/replication thread)"
    continue
  fi

  echo "${LOG_PREFIX} KILLING thread ${THREAD_ID} (blocking ${BLOCK_SEC}s, query: ${BLOCK_QUERY:-<idle>})"

  # 先嘗試 KILL QUERY（只取消當前語句，不斷連線）
  kubectl --context="${KUBE_CONTEXT}" -n "${NAMESPACE}" \
    exec "${POD_NAME}" -c "${CONTAINER}" -- \
    bash -c "MYSQL_PWD='${MYSQL_PWD}' mysql -u root -e 'KILL QUERY ${THREAD_ID};'" 2>/dev/null || true

  sleep 2

  # 檢查是否仍在 blocking
  STILL_BLOCKING=$(kubectl --context="${KUBE_CONTEXT}" -n "${NAMESPACE}" \
    exec "${POD_NAME}" -c "${CONTAINER}" -- \
    bash -c "MYSQL_PWD='${MYSQL_PWD}' mysql -u root -sN -e \"
      SELECT COUNT(*) FROM information_schema.innodb_lock_waits lw
      JOIN information_schema.innodb_trx b ON b.trx_id = lw.blocking_trx_id
      WHERE b.trx_mysql_thread_id = ${THREAD_ID};\"" 2>/dev/null || echo "0")

  if [ "${STILL_BLOCKING}" -gt 0 ] 2>/dev/null; then
    echo "${LOG_PREFIX} Thread ${THREAD_ID} still blocking, executing KILL CONNECTION"
    kubectl --context="${KUBE_CONTEXT}" -n "${NAMESPACE}" \
      exec "${POD_NAME}" -c "${CONTAINER}" -- \
      bash -c "MYSQL_PWD='${MYSQL_PWD}' mysql -u root -e 'KILL CONNECTION ${THREAD_ID};'" 2>/dev/null || true
  else
    echo "${LOG_PREFIX} Thread ${THREAD_ID} resolved after KILL QUERY"
  fi
done

echo "${LOG_PREFIX} Done"
```

### Crontab 設定

```bash
# 每 5 分鐘檢查一次，殺 blocking 超過 300 秒的 transaction
*/5 * * * * /path/to/kill_blocking_trx.sh >> /var/log/kill_blocking_trx.log 2>&1
```

### 安全注意事項

| 項目 | 說明 |
|------|------|
| **thread_id > 10** | 跳過系統和複製 thread，避免殺掉 replication |
| **blocking > 300 秒** | 只殺長時間 blocking 的 transaction，短暫 lock 不處理 |
| **先 KILL QUERY** | 只取消當前語句，讓連線繼續存在（對 App 衝擊最小） |
| **再 KILL CONNECTION** | 如果 KILL QUERY 無效（例如 idle transaction 沒有 running query），才斷連線 |
| **Log 記錄** | 每次 KILL 都記錄 thread ID、blocking 時間、query 內容，方便事後追溯 |
| **不殺 waiting thread** | 只殺 **blocker**（持有 lock 的人），不殺 waiter（等待的人） |

### KILL QUERY vs KILL CONNECTION

```
KILL QUERY 123;       → 取消 thread 123 當前正在執行的 SQL
                        連線保持，App 可以重試
                        適合：blocking query 正在 running

KILL CONNECTION 123;  → 斷開 thread 123 的整個連線
                        App 的連線被強制關閉，需要重連
                        適合：idle transaction（沒有 running query，但持有 lock）
```

### 調整建議

| 參數 | 預設值 | 調整建議 |
|------|--------|---------|
| `MAX_BLOCK_SEC` | 300（5 分鐘） | 如果是 OLTP 系統，可降到 60-120 秒 |
| crontab 頻率 | `*/5`（每 5 分鐘） | 配合 `MAX_BLOCK_SEC` 調整，確保不會漏掉 |
| `thread_id > 10` | 10 | 如果有特殊系統 thread，可以調高 |

---

## 四、Alert 總覽

### 通用 DB 健康 Alerts（Platform 預設提供）

| Alert | 偵測什麼 | for | Severity | group_name |
|-------|---------|-----|----------|------------|
| MariaDBThreadsRunningHigh | threads_running > 10 | 2m | warning | all |
| MariaDBThreadsRunningCritical | threads_running > 30 | 1m | warning | platform |
| MariaDBSlowQueriesSpike | slow queries > 0.5/s | 2m | info | user |
| MariaDBCPUThrottlingHigh | CPU throttle > 25% | 5m | warning | platform |
| MariaDBRowLockContention | 持續 3+ 個 thread 等待 row lock | 2m | warning | all |
| MariaDBRowLockWaitSpike | 5 分鐘內 lock wait 增加 > 10 次 | 0m | warning | all |
| MariaDBRowLockTimeHigh | 最近 5 分鐘平均 lock wait 時間 > 5 秒 | 1m | critical | all |
| MariaDBRowLockSevere | 10+ 個 thread 同時等待 row lock | 30s | critical | platform |

### QPS Alerts（Optional — App team 自行啟用）

| Alert | 偵測什麼 | 適用情境 |
|-------|---------|---------|
| MariaDBQPSSpike | Client QPS > 1.5x 基線 | 穩定流量的 OLTP/API app |
| MariaDBQPSDrop | Client QPS < 0.5x 基線 | 24/7 持續有流量的 app |
| MariaDBQPSHigh | Client QPS > 固定門檻 | Batch/periodic job app |
