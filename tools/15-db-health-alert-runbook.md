# DB Health Alert Runbook

Platform team 收到 alert 後的處理 SOP。
沒有自動擴容機制，緊急的 platform 直接處理，不緊急的通知 user 提 ticket。

---

## 一、MariaDBThreadsRunningHigh

| 項目 | 內容 |
|------|------|
| **觸發條件** | `mysql_global_status_threads_running > 10` 持續 2 分鐘 |
| **緊急度** | 中 |
| **group_name** | all（platform + user） |

### Platform 處理步驟

```bash
# 1. 確認哪些 query 在跑
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "SHOW PROCESSLIST;" | grep -v Sleep

# 2. 找出跑最久的 query
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "SELECT id, user, host, db, time, state, LEFT(info, 80) AS query FROM information_schema.processlist WHERE command != 'Sleep' ORDER BY time DESC LIMIT 10;"
```

### 判斷與行動

| 看到什麼 | 處理 |
|---------|------|
| 大量相同 query | 通知 app team：可能缺 index 或 N+1 query |
| 大量 Lock wait | 見 MariaDBRowLockContention runbook |
| 正常 query 但量太大 | 通知 app team 降低並發或申請擴容 |

### 通知 User 範本

```
[Alert] MariaDB threads_running 偏高
Namespace: <NS>
Pod: <POD>
目前 threads_running: <VALUE>

請檢查以下 query 是否有優化空間：
<附上 PROCESSLIST 結果>

建議：
- 檢查 slow query log（見 05-slow-query-log-scripts.md）
- 確認是否有缺 index 的 query
- 如需擴容請提 ticket
```

---

## 二、MariaDBThreadsRunningCritical

| 項目 | 內容 |
|------|------|
| **觸發條件** | `mysql_global_status_threads_running > 30` 持續 1 分鐘 |
| **緊急度** | 高 |
| **group_name** | platform |

### Platform 立即處理

```bash
# 1. 確認 DB 是否還能回應
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "SELECT 1;"

# 2. 找出最久的 query（優先 KILL 候選）
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "SELECT id, user, time, state, LEFT(info, 80) AS query FROM information_schema.processlist WHERE command != 'Sleep' ORDER BY time DESC LIMIT 10;"

# 3. 止血：KILL 跑最久的 query（先 KILL QUERY，保留連線）
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "KILL QUERY <THREAD_ID>;"

# 4. 如果 KILL QUERY 無效（idle transaction 持有 lock）
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "KILL CONNECTION <THREAD_ID>;"
```

### 判斷與行動

| 情境 | 處理 |
|------|------|
| DB 能回應 | KILL 最久的 query → 通知 user |
| DB 無法回應 | 檢查是否 deadlock（SHOW ENGINE INNODB STATUS）→ 考慮 failover |
| 持續發生 | 通知 user 降低流量 + 啟用 kill_blocking_trx.sh（見 14-qps-and-lock-remediation.md 第三節） |

---

## 三、MariaDBSlowQueriesSpike

| 項目 | 內容 |
|------|------|
| **觸發條件** | `rate(mysql_global_status_slow_queries[5m]) > 0.5` 持續 2 分鐘 |
| **緊急度** | 低 |
| **group_name** | user |

### Platform 處理步驟

```bash
# 1. 確認是否影響 DB 健康
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "SHOW GLOBAL STATUS LIKE 'Threads_running';"

# 2. 如果 threads_running 正常 → 轉給 user，不需要 platform 介入
# 3. 如果 threads_running 也高 → 升級為 ThreadsRunningHigh 處理流程
```

### 通知 User 範本

```
[Info] MariaDB slow queries 增加
Namespace: <NS>
Pod: <POD>
目前速率: <VALUE> slow queries/sec

請檢查 slow query log：
  kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
    bash -c "tail -50 <slow_log_path>"

建議：
- 用 show_slow_queries.sh 分析（見 05-slow-query-log-scripts.md）
- 檢查是否缺 index（見 01-no-index-monitoring-concepts.md）
- 參考 SQL 優化指南（見 03-sql-tuning-guide.md）
```

---

## 四、MariaDBCPUThrottlingHigh

| 項目 | 內容 |
|------|------|
| **觸發條件** | `sum by (namespace, pod, container) (rate(container_cpu_cfs_throttled_seconds_total{container="mariadb"}[5m])) > 0.25` 持續 5 分鐘 |
| **緊急度** | 中-高 |
| **group_name** | platform |

### Platform 處理步驟

```bash
# 1. 查目前的 resource limits
kubectl --context=<CTX> -n <NS> get pod <POD> -o jsonpath='{.spec.containers[?(@.name=="mariadb")].resources}' | jq .

# 2. 查目前 CPU 使用率
kubectl --context=<CTX> -n <NS> top pod <POD> --containers

# 3. 查 threads_running 判斷是正常負載還是異常
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "SHOW GLOBAL STATUS LIKE 'Threads_running';"
```

### 判斷與行動

| 情境 | 處理 |
|------|------|
| CPU limit 太低 + threads_running 正常 | **Platform 直接調高 CPU limit** |
| CPU limit 合理 + threads_running 高 | 先處理 ThreadsRunningHigh（app 問題導致 CPU 高） |
| 長期 throttling | review DB sizing，考慮升級規格（small → medium → large） |

### 不需要通知 User

CPU resource limit 是 platform 管理的範圍，直接調整即可。
除非 throttling 是因為 user 的異常 query 導致，才需要通知。

---

## 五、MariaDBRowLockContention

| 項目 | 內容 |
|------|------|
| **觸發條件** | `mysql_global_status_innodb_row_lock_current_waits > 3` 持續 2 分鐘 |
| **緊急度** | 中 |
| **group_name** | all |

### Platform 處理步驟

```bash
# 1. 找出誰 block 誰
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "
SELECT r.trx_mysql_thread_id AS waiting_thread,
       b.trx_mysql_thread_id AS blocking_thread,
       TIMESTAMPDIFF(SECOND, b.trx_started, NOW()) AS blocking_sec,
       b.trx_query AS blocking_query
FROM information_schema.innodb_lock_waits lw
JOIN information_schema.innodb_trx b ON b.trx_id = lw.blocking_trx_id
JOIN information_schema.innodb_trx r ON r.trx_id = lw.requesting_trx_id
ORDER BY blocking_sec DESC;"
```

### 判斷與行動

| 情境 | 處理 |
|------|------|
| Blocking < 5 分鐘 | 觀察，可能自行解決 |
| Blocking > 5 分鐘 | 通知 user：你的 transaction 沒 commit |
| 頻繁發生 | 建議啟用 kill_blocking_trx.sh（見 14-qps-and-lock-remediation.md 第三節） |

### 通知 User 範本

```
[Alert] MariaDB row lock contention
Namespace: <NS>
Pod: <POD>
Blocking thread: <THREAD_ID>（已 blocking <N> 秒）

你的 transaction 可能忘記 COMMIT。
Blocking query: <QUERY>

建議：
- 確認 app 的 transaction 是否正確 COMMIT/ROLLBACK
- 減少長 transaction（將大批更新拆成小批次）
- 參考 11-row-lock-diagnosis.md
```

---

## 六、MariaDBRowLockSevere

| 項目 | 內容 |
|------|------|
| **觸發條件** | `mysql_global_status_innodb_row_lock_current_waits > 10` 持續 30 秒 |
| **緊急度** | 高 |
| **group_name** | platform |

### Platform 立即處理

```bash
# 1. 找出所有 blocking thread
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "
SELECT DISTINCT b.trx_mysql_thread_id AS blocking_thread,
       TIMESTAMPDIFF(SECOND, b.trx_started, NOW()) AS blocking_sec,
       b.trx_query AS blocking_query
FROM information_schema.innodb_lock_waits lw
JOIN information_schema.innodb_trx b ON b.trx_id = lw.blocking_trx_id
ORDER BY blocking_sec DESC;"

# 2. KILL 最久的 blocking thread
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "KILL QUERY <BLOCKING_THREAD_ID>;"

# 3. 如果仍然 blocking（idle transaction）
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "KILL CONNECTION <BLOCKING_THREAD_ID>;"
```

### 後續

- 通知 app team 修正 transaction 邏輯
- 考慮啟用自動殺 blocking 腳本

---

## 七、MariaDBRowLockWaitSpike

| 項目 | 內容 |
|------|------|
| **觸發條件** | `increase(mysql_global_status_innodb_row_lock_waits[5m]) > 3 * avg_over_time(...[7d:5m] offset 1d) + 10`（最近 5 分鐘 > 7 天 baseline × 3）|
| **緊急度** | 低 |
| **group_name** | all |

### Platform 處理步驟

```bash
# 1. 確認是否有持續的 lock contention
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_current_waits';"

# 2. 如果 current_waits = 0 → 暫時性 spike，已自行解決，觀察即可
# 3. 如果 current_waits > 0 → 見 RowLockContention runbook
```

### 通知 User（可選）

如果頻繁發生，通知 user review transaction 設計。否則觀察即可。

---

## 八、MariaDBRowLockTimeHigh

| 項目 | 內容 |
|------|------|
| **觸發條件** | `increase(row_lock_time[5m]) / clamp_min(increase(row_lock_waits[5m]), 1) > 5000` 持續 1 分鐘（最近 5 分鐘平均 lock wait > 5s）|
| **緊急度** | 高 |
| **group_name** | all |

### Platform 處理步驟

```bash
# 1. 確認平均等待時間
kubectl --context=<CTX> -n <NS> exec <POD> -c mariadb -- \
  mysql -u root -e "SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_time%';"

# 2. 找出 blocking transaction（同 RowLockContention runbook）
# 3. 如果 blocking > 5 分鐘 → KILL
```

### 判斷與行動

| 情境 | 處理 |
|------|------|
| 單一 long transaction blocking | KILL 該 thread → 通知 user |
| 多個 transaction 互相等待 | 可能 deadlock → SHOW ENGINE INNODB STATUS 確認 |
| 平均等待時間持續高 | user 的 transaction 設計有問題，通知 user 優化 |

---

## 九、處理流程總結

```
Alert 觸發
  │
  ├─ 緊急（Critical / Severe）
  │   → Platform 立即介入
  │   → KILL query / 調 resource
  │   → 事後通知 User
  │
  ├─ 中等（High / Warning）
  │   → Platform 診斷根因
  │   ├─ DB 資源問題 → Platform 直接處理
  │   └─ App 問題 → 通知 User + 附診斷資訊
  │
  └─ 低（Info）
      → 確認不影響 DB 健康
      → 轉給 User 處理
```

### Alert 快速查找表

| Alert | 緊急度 | 誰先動 | User 需要做什麼 |
|-------|:---:|--------|---------------|
| ThreadsRunningHigh | 中 | Platform 診斷 → 通知 User | 優化 query / 減少並發 |
| ThreadsRunningCritical | **高** | **Platform 立即 KILL** | 優化 query / 申請擴容 |
| SlowQueriesSpike | 低 | User | 檢查 slow log / 加 index |
| CPUThrottlingHigh | 中高 | **Platform 調 resource** | 通常不需要 |
| RowLockContention | 中 | Platform 診斷 → 通知 User | 修正 transaction 邏輯 |
| RowLockSevere | **高** | **Platform 立即 KILL** | 修正 transaction 邏輯 |
| RowLockWaitSpike | 低 | 觀察 | 如果頻繁：review transaction |
| RowLockTimeHigh | **高** | Platform KILL → 通知 User | 優化 transaction |
