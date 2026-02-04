# Semi-Sync Replication 深入解析

## Async vs Semi-sync 比較

### Asynchronous Replication

```
Client              Primary                 Replica
  │                    │                       │
  │── COMMIT ────────► │                       │
  │                    │                       │
  │               ┌────┴────┐                  │
  │               │ Write   │                  │
  │               │ binlog  │                  │
  │               └────┬────┘                  │
  │               ┌────┴────┐                  │
  │               │ InnoDB  │                  │
  │               │ COMMIT  │                  │
  │               └────┬────┘                  │
  │                    │                       │
  │◄─── OK ────────────│                       │
  │                    │                       │
  │                    │── Send binlog ──────► │ (背景)
  │                    │                       │
```

**特點**:
- Client 收到 OK 時，replica 可能還沒收到資料
- Primary crash 時可能遺失已確認的 transactions
- 效能最好，延遲最低

### Semi-sync AFTER_COMMIT (舊版預設)

```
Client              Primary                 Replica
  │                    │                       │
  │── COMMIT ────────► │                       │
  │                    │                       │
  │               ┌────┴────┐                  │
  │               │ Write   │                  │
  │               │ binlog  │                  │
  │               └────┬────┘                  │
  │               ┌────┴────┐                  │
  │               │ InnoDB  │                  │
  │               │ COMMIT  │◄─────────────────┤ 問題：已經 commit 了！
  │               └────┬────┘                  │
  │                    │                       │
  │                    │── Send binlog ──────► │
  │                    │                       │
  │                    │◄─── ACK ──────────────│
  │                    │                       │
  │◄─── OK ────────────│                       │
```

**問題**:
- Commit 在 send binlog 之前
- 如果 commit 後、send 前 crash → phantom read
- 其他 session 可能已經看到這筆資料

### Semi-sync AFTER_SYNC (推薦，MariaDB 10.3+ 預設)

```
Client              Primary                 Replica
  │                    │                       │
  │── COMMIT ────────► │                       │
  │                    │                       │
  │               ┌────┴────┐                  │
  │               │ Write   │                  │
  │               │ binlog  │                  │
  │               └────┬────┘                  │
  │                    │                       │
  │                    │── Send binlog ──────► │
  │                    │                       │
  │                    │◄─── ACK ──────────────│
  │                    │                       │
  │               ┌────┴────┐                  │
  │               │ InnoDB  │◄─────────────────┤ 重點：收到 ACK 才 commit！
  │               │ COMMIT  │                  │
  │               └────┬────┘                  │
  │                    │                       │
  │◄─── OK ────────────│                       │
```

**優點**:
- 沒有 phantom read 問題
- 如果 ACK 前 crash，InnoDB 會 rollback
- Replica 一定有已確認的資料

---

## Semi-sync Metrics 完整解說

### 狀態變數一覽

```sql
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync%';
```

### 核心 Metrics

| 變數名稱 | 類型 | 說明 |
|---------|------|------|
| `Rpl_semi_sync_master_status` | 狀態 | ON = semi-sync 啟用中, OFF = 降級為 async |
| `Rpl_semi_sync_master_yes_tx` | 計數器 | 成功收到 ACK 的 transactions 累計數 |
| `Rpl_semi_sync_master_no_tx` | 計數器 | 未收到 ACK (timeout) 的 transactions 累計數 |
| `Rpl_semi_sync_master_clients` | 數量 | 目前連接的 semi-sync replica 數量 |

### `_status` - 即時狀態

```sql
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_status';
```

| 值 | 意義 | 原因 |
|----|------|------|
| ON | Semi-sync 正常運作 | 至少有一個 replica 連接 |
| OFF | 已降級為 async | timeout 或沒有 replica |

**降級條件**:
1. 所有 replica 都斷線
2. 等待 ACK 超時 (`rpl_semi_sync_master_timeout`)

### `_yes_tx` / `_no_tx` - 累計計數器

```sql
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_%_tx';

+------------------------------------+-------+
| Variable_name                      | Value |
+------------------------------------+-------+
| Rpl_semi_sync_master_yes_tx        | 12345 |
| Rpl_semi_sync_master_no_tx         | 79    |
+------------------------------------+-------+
```

**重要概念**:
- 這是**累計**計數器，從 server 啟動就開始計算
- 重啟會歸零
- 要監控**增量**，不是絕對值

**使用方式**:
```sql
-- 記錄 baseline
SELECT @no_tx_before := VARIABLE_VALUE
FROM information_schema.GLOBAL_STATUS
WHERE VARIABLE_NAME = 'Rpl_semi_sync_master_no_tx';

-- 稍後檢查增量
SELECT VARIABLE_VALUE - @no_tx_before as no_tx_delta
FROM information_schema.GLOBAL_STATUS
WHERE VARIABLE_NAME = 'Rpl_semi_sync_master_no_tx';
```

### `_clients` - Replica 連線數

```sql
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_clients';
```

| 值 | 意義 |
|----|------|
| 0 | 沒有 semi-sync replica，會降級 |
| 1 | 最低配置，有 SPOF |
| 2+ | 推薦，有冗餘 |

### 其他重要 Metrics

```sql
-- 等待 ACK 的平均時間 (微秒)
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_tx_avg_wait_time';

-- 等待超過一次的 transactions
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_wait_sessions';

-- 目前正在等待 ACK 的 sessions
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_wait_pos_backtraverse';

-- 網路等待次數
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_net_waits';

-- 網路等待平均時間
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_net_avg_wait_time';
```

---

## 確保 Semi-sync 不降級

### 問題：Semi-sync 降級為 Async

當發生以下情況，semi-sync 會自動降級為 async：
1. 沒有 replica 連接
2. 等待 ACK 超時
3. 所有 replica 都太慢

### 解決方案

#### 1. 增加 Timeout

```sql
-- 預設是 10 秒
SET GLOBAL rpl_semi_sync_master_timeout = 30000;  -- 30 秒

-- 或設為超大值（不推薦，可能造成寫入卡住）
SET GLOBAL rpl_semi_sync_master_timeout = 4294967295;  -- 約 50 天
```

**權衡**: Timeout 太大會導致 replica 掛掉時寫入卡住

#### 2. 設定 wait_for_slave_count

```sql
-- 至少等待 N 個 replica 確認
SET GLOBAL rpl_semi_sync_master_wait_for_slave_count = 1;  -- 預設
SET GLOBAL rpl_semi_sync_master_wait_for_slave_count = 2;  -- 更安全
```

**注意**: 如果設為 2，至少需要 2 個 replica 才能正常運作

#### 3. wait_no_slave 設定

```sql
-- 沒有 replica 時的行為
SET GLOBAL rpl_semi_sync_master_wait_no_slave = ON;   -- 等待直到有 replica
SET GLOBAL rpl_semi_sync_master_wait_no_slave = OFF;  -- 直接降級為 async
```

### 配置建議

**3 節點 cluster**:
```sql
rpl_semi_sync_master_timeout = 10000        -- 10 秒
rpl_semi_sync_master_wait_for_slave_count = 1
rpl_semi_sync_master_wait_no_slave = ON
```

**5 節點 cluster (高可用)**:
```sql
rpl_semi_sync_master_timeout = 10000        -- 10 秒
rpl_semi_sync_master_wait_for_slave_count = 2
rpl_semi_sync_master_wait_no_slave = ON
```

---

## 監控與 Alert

### Prometheus Metrics

MariaDB Operator 暴露的 metrics:

```yaml
# Semi-sync 狀態
mysql_global_status_rpl_semi_sync_master_status{...} 1  # 1=ON, 0=OFF

# Transaction 計數
mysql_global_status_rpl_semi_sync_master_yes_tx{...} 12345
mysql_global_status_rpl_semi_sync_master_no_tx{...} 79

# Replica 連線數
mysql_global_status_rpl_semi_sync_master_clients{...} 2

# 等待時間
mysql_global_status_rpl_semi_sync_master_tx_avg_wait_time{...} 1234
```

### Alert Rules

#### 1. Semi-sync 降級 Alert

```yaml
groups:
- name: mariadb-semi-sync
  rules:
  - alert: MariaDBSemiSyncDisabled
    expr: mysql_global_status_rpl_semi_sync_master_status == 0
    for: 1m
    labels:
      severity: warning
    annotations:
      summary: "MariaDB semi-sync disabled on {{ $labels.instance }}"
      description: "Semi-sync has been disabled for more than 1 minute. Data loss risk increased."
```

#### 2. no_tx 增加 Alert

```yaml
  - alert: MariaDBSemiSyncNoTxIncreasing
    expr: increase(mysql_global_status_rpl_semi_sync_master_no_tx[5m]) > 0
    for: 0m
    labels:
      severity: warning
    annotations:
      summary: "MariaDB semi-sync no_tx increasing on {{ $labels.instance }}"
      description: "{{ $value }} transactions committed without semi-sync ACK in the last 5 minutes."
```

#### 3. 沒有 Replica 連線

```yaml
  - alert: MariaDBSemiSyncNoClients
    expr: mysql_global_status_rpl_semi_sync_master_clients == 0
    for: 30s
    labels:
      severity: critical
    annotations:
      summary: "No semi-sync replicas connected to {{ $labels.instance }}"
      description: "Primary has no semi-sync replicas. Failover capability compromised."
```

#### 4. ACK 等待時間過長

```yaml
  - alert: MariaDBSemiSyncSlowAck
    expr: mysql_global_status_rpl_semi_sync_master_tx_avg_wait_time > 100000
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "Semi-sync ACK latency high on {{ $labels.instance }}"
      description: "Average ACK wait time is {{ $value }}μs (>100ms). Check network or replica performance."
```

### no_tx Alert Runbook

當收到 `MariaDBSemiSyncNoTxIncreasing` Alert:

```bash
# 1. 確認 alert 是真的
kubectl exec mariadb-0 -- mysql -e \
  "SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_no_tx';"

# 2. 檢查 semi-sync 狀態
kubectl exec mariadb-0 -- mysql -e \
  "SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_status';"

# 3. 檢查 replica 連線數
kubectl exec mariadb-0 -- mysql -e \
  "SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_clients';"

# 4. 檢查各 replica 狀態
for i in 1 2; do
  echo "=== mariadb-$i ==="
  kubectl exec mariadb-$i -- mysql -e "SHOW SLAVE STATUS\G" | \
    grep -E "(Slave_IO|Slave_SQL|Seconds_Behind)"
done

# 5. 檢查網路延遲
kubectl exec mariadb-0 -- ping -c 3 mariadb-1.mariadb-internal

# 6. 如果 replica 斷線，重啟 replication
kubectl exec mariadb-1 -- mysql -e "STOP SLAVE; START SLAVE;"
```

---

## 最佳實踐

### 1. 定期監控 no_tx 趨勢

```sql
-- 每小時記錄 no_tx 值
INSERT INTO monitoring.semi_sync_metrics (timestamp, no_tx)
SELECT NOW(), VARIABLE_VALUE
FROM information_schema.GLOBAL_STATUS
WHERE VARIABLE_NAME = 'Rpl_semi_sync_master_no_tx';
```

### 2. 設定合理的 Timeout

- 太短：頻繁降級，資料遺失風險
- 太長：replica 故障時寫入卡住

推薦：10-30 秒，根據網路狀況調整

### 3. 確保足夠的 Replica

- 生產環境至少 2 個 replica
- 考慮 `wait_for_slave_count = 2` 配合 3+ replicas

### 4. Binlog 設定配合

```sql
-- 確保 binlog 格式支援
SET GLOBAL binlog_format = 'ROW';

-- 確保 sync_binlog 設定
SET GLOBAL sync_binlog = 1;  -- 每次 commit 都 fsync

-- 確保 InnoDB 設定
SET GLOBAL innodb_flush_log_at_trx_commit = 1;
```

### 5. 理解降級行為

Semi-sync 降級後會自動恢復，但要注意：
- 降級期間的 transactions 沒有 replica 確認
- 恢復後不會重傳降級期間的資料
- 需要額外的監控來追蹤這個 window
