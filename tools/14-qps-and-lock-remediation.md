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

### Alert Rules

```yaml
# QPS spike: client QPS exceeds 1.5x the 1-hour baseline
- alert: MariaDBQPSSpike
  expr: rate(mysql_global_status_questions[5m]) > 1.5 * avg_over_time(rate(mysql_global_status_questions[5m])[1h:1m])
  for: 2m
  labels:
    severity: warning
  annotations:
    summary: "MariaDB client QPS spike detected"
    description: "{{ $labels.instance }} client QPS exceeds 1.5x the 1-hour average. Check for app traffic surge or runaway queries."

# QPS drop: sudden drop may indicate app connection failure or upstream issue
- alert: MariaDBQPSDrop
  expr: rate(mysql_global_status_questions[5m]) < 0.5 * avg_over_time(rate(mysql_global_status_questions[5m])[1h:1m]) and rate(mysql_global_status_questions[5m]) > 0
  for: 3m
  labels:
    severity: warning
  annotations:
    summary: "MariaDB client QPS dropped below 50% of baseline"
    description: "{{ $labels.instance }} client QPS dropped significantly. Check app connectivity and upstream services."
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
# Sustained row lock contention (multiple threads waiting for over 2 minutes)
- alert: MariaDBRowLockContention
  expr: mysql_global_status_innodb_row_lock_current_waits > 3
  for: 2m
  labels:
    severity: warning
  annotations:
    summary: "Sustained row lock contention on {{ $labels.instance }}"
    description: "{{ $labels.instance }} has {{ $value }} threads waiting for row locks for over 2 minutes. Run check_row_locks.sh to identify blocking SQL. See 11-row-lock-diagnosis.md."

# Row lock wait frequency spike (more than 10 new waits in 5 minutes)
- alert: MariaDBRowLockWaitSpike
  expr: increase(mysql_global_status_innodb_row_lock_waits_total[5m]) > 10
  for: 0m
  labels:
    severity: warning
  annotations:
    summary: "Row lock wait spike on {{ $labels.instance }}"
    description: "{{ $labels.instance }} had {{ $value | printf \"%.0f\" }} new row lock waits in the last 5 minutes. Possible batch job or hot-row contention."

# Average lock wait time too high (over 5 seconds)
- alert: MariaDBRowLockTimeHigh
  expr: mysql_global_status_innodb_row_lock_time_avg > 5000
  for: 1m
  labels:
    severity: critical
  annotations:
    summary: "Average row lock wait time exceeds 5s on {{ $labels.instance }}"
    description: "{{ $labels.instance }} average row lock wait is {{ $value }}ms. Transactions are being significantly delayed. Immediate investigation needed."

# Severe contention: many threads blocked simultaneously
- alert: MariaDBRowLockSevere
  expr: mysql_global_status_innodb_row_lock_current_waits > 10
  for: 30s
  labels:
    severity: critical
  annotations:
    summary: "Severe row lock contention on {{ $labels.instance }}"
    description: "{{ $labels.instance }} has {{ $value }} threads waiting for row locks. This may cause cascading timeouts and app errors."
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

| Alert | 偵測什麼 | for | Severity |
|-------|---------|-----|----------|
| MariaDBQPSSpike | Client QPS 超過基線 1.5 倍 | 2m | warning |
| MariaDBQPSDrop | Client QPS 跌到基線的 50% 以下 | 3m | warning |
| MariaDBRowLockContention | 持續 3+ 個 thread 等待 row lock | 2m | warning |
| MariaDBRowLockWaitSpike | 5 分鐘內 lock wait 增加 > 10 次 | 0m | warning |
| MariaDBRowLockTimeHigh | 平均 lock wait 時間 > 5 秒 | 1m | critical |
| MariaDBRowLockSevere | 10+ 個 thread 同時等待 row lock | 30s | critical |
