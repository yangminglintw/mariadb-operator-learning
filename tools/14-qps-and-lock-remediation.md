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

# === Replication Health Alerts ===

# IO thread down — replication fully disconnected
- alert: MariaDBReplicationIOThreadDown
  expr: mysql_slave_status_slave_io_running == 0 and ON (instance) mysql_slave_status_master_server_id > 0
  for: 30s
  labels:
    severity: info
    group_name: platform
  annotations:
    summary: "Replication IO thread down on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} IO thread stopped. Replication is fully disconnected."

# SQL thread down — relay log not being applied
- alert: MariaDBReplicationSQLThreadDown
  expr: mysql_slave_status_slave_sql_running == 0 and ON (instance) mysql_slave_status_master_server_id > 0
  for: 30s
  labels:
    severity: info
    group_name: platform
  annotations:
    summary: "Replication SQL thread down on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} SQL thread stopped. Relay log is not being applied."

# Replication error — SQL error, possible data inconsistency
- alert: MariaDBReplicationError
  expr: mysql_slave_status_last_errno != 0 and ON (instance) mysql_slave_status_master_server_id > 0
  for: 1m
  labels:
    severity: info
    group_name: platform
  annotations:
    summary: "Replication error on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} replication error. SQL thread may be stopped."

# Replication lag anomaly — exceeds 3x 7-day P95 baseline
- alert: MariaDBReplicationLagWarning
  expr: (mysql_slave_status_seconds_behind_master - mysql_slave_status_sql_delay) > 3 * quantile_over_time(0.95, (mysql_slave_status_seconds_behind_master - mysql_slave_status_sql_delay)[7d:1m] offset 1d) + 10 and ON (instance) mysql_slave_status_master_server_id > 0
  for: 2m
  labels:
    severity: info
    group_name: all
  annotations:
    summary: "Replication lag anomaly on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} lag {{ $value }}s exceeds 3x its 7-day P95 baseline."

# Replication lag critical — hard ceiling regardless of baseline
- alert: MariaDBReplicationLagCritical
  expr: (mysql_slave_status_seconds_behind_master - mysql_slave_status_sql_delay) > 300 and ON (instance) mysql_slave_status_master_server_id > 0
  for: 1m
  labels:
    severity: info
    group_name: all
  annotations:
    summary: "Replication lag > 5min on {{ $labels.namespace }}/{{ $labels.pod }}"
    description: "{{ $labels.namespace }}/{{ $labels.pod }} replication lag is {{ $value }}s. Data inconsistency risk."
```

---

## 二、Alert 總覽

### 通用 DB 健康 Alerts（Platform 預設提供）

| Alert | 偵測什麼 | for | Severity | group_name |
|-------|---------|-----|----------|------------|
| MariaDBThreadsRunningHigh | threads_running > 10 | 2m | warning | all |
| MariaDBThreadsRunningCritical | threads_running > 30 | 1m | warning | platform |
| MariaDBSlowQueriesSpike | slow queries > 0.5/s | 2m | info | user |
| MariaDBCPUThrottlingHigh | CPU throttle > 25% | 5m | warning | platform |
| MariaDBReplicationIOThreadDown | IO thread 停了 = replication 斷開 | 30s | info | platform |
| MariaDBReplicationSQLThreadDown | SQL thread 停了 = relay log 不在 apply | 30s | info | platform |
| MariaDBReplicationError | Replication SQL error | 1m | info | platform |
| MariaDBReplicationLagWarning | Lag 超過 7 天 P95 baseline × 3 | 2m | info | all |
| MariaDBReplicationLagCritical | Lag > 300s（固定硬底線）| 1m | info | all |

> Row lock 相關 alert 見 [`11-row-lock-diagnosis.md`](11-row-lock-diagnosis.md) 第十一節。
> Semi-sync 相關 alert 見 [`12-semi-sync-and-error-1236.md`](12-semi-sync-and-error-1236.md)。

### QPS Alerts（Optional — App team 自行啟用）

| Alert | 偵測什麼 | 適用情境 |
|-------|---------|---------|
| MariaDBQPSSpike | Client QPS > 1.5x 基線 | 穩定流量的 OLTP/API app |
| MariaDBQPSDrop | Client QPS < 0.5x 基線 | 24/7 持續有流量的 app |
| MariaDBQPSHigh | Client QPS > 固定門檻 | Batch/periodic job app |
