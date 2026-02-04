# MariaDB Operator Replication 設定

## Server ID 產生規則

每台 MariaDB instance 需要唯一的 server_id：

```go
// pkg/controller/configmap/configmap.go
serverID := 10 + podIndex
```

| Pod | Server ID |
|-----|-----------|
| mariadb-0 | 10 |
| mariadb-1 | 11 |
| mariadb-2 | 12 |

為什麼從 10 開始？避免與預設值 (0 或 1) 衝突。

## my.cnf 設定內容

Operator 透過 ConfigMap 注入設定：

```ini
[mariadbd]
server_id=11
log_bin=mariadb-bin
binlog_format=ROW
log_slave_updates=ON

# GTID 設定
gtid_strict_mode=ON
gtid_domain_id=0

# Semi-sync replication
rpl_semi_sync_master_enabled=ON
rpl_semi_sync_slave_enabled=ON
rpl_semi_sync_master_wait_point=AFTER_SYNC
rpl_semi_sync_master_timeout=10000
```

### 重要設定說明

| 設定 | 值 | 說明 |
|------|-----|------|
| `log_bin` | ON | 啟用 binary log |
| `binlog_format` | ROW | Row-based replication |
| `log_slave_updates` | ON | Replica 也寫 binlog |
| `gtid_strict_mode` | ON | 嚴格 GTID 模式 |

## ConfigurePrimary() 流程

當一台 server 被設定為 primary 時：

```go
// pkg/controller/galera/replication.go
func (r *ReplicationConfig) ConfigurePrimary(ctx context.Context, client *sql.Client) error {
    // 1. 停止 replica (如果之前是 replica)
    client.StopAllSlaves(ctx)

    // 2. 重置 replica 設定
    client.ResetAllSlaves(ctx)

    // 3. 重置 master (清除 binlog state)
    client.ResetMaster(ctx)

    // 4. 設定為 read-write
    client.SetReadOnly(ctx, false)

    return nil
}
```

### 為什麼要 RESET MASTER？

```
情境：Pod-1 原本是 replica，現在要變成 primary

執行前：
  gtid_binlog_pos:   0-11-50   (本地的 transaction)
  gtid_slave_pos:    0-10-100  (從舊 primary 複製的)
  gtid_binlog_state: 0-11-50

執行 RESET MASTER 後：
  gtid_binlog_pos:   (空)
  gtid_slave_pos:    0-10-100  (保留！)
  gtid_binlog_state: (空)
  gtid_current_pos:  0-10-100  (來自 slave_pos)
```

這樣其他 replica 可以用 `gtid_current_pos` 知道新 primary 的狀態。

## ConfigureReplica() 流程

當一台 server 被設定為 replica 時：

```go
// pkg/controller/galera/replication.go
func (r *ReplicationConfig) ConfigureReplica(
    ctx context.Context,
    client *sql.Client,
    primary *ReplicationState,
) error {
    // 1. 設定為 read-only
    client.SetReadOnly(ctx, true)

    // 2. 停止現有 replication
    client.StopAllSlaves(ctx)

    // 3. 設定 master info
    client.ChangeMaster(ctx, &ChangeMasterOpts{
        Host:     primary.PodIP,
        Port:     3306,
        User:     "repl",
        Password: replPassword,
        UseGTID:  "slave_pos",
    })

    // 4. 啟動 replication
    client.StartSlave(ctx)

    return nil
}
```

### CHANGE MASTER 語法

```sql
CHANGE MASTER TO
    MASTER_HOST='10.0.0.10',
    MASTER_PORT=3306,
    MASTER_USER='repl',
    MASTER_PASSWORD='xxx',
    MASTER_USE_GTID=slave_pos;
```

`MASTER_USE_GTID=slave_pos` 表示：
- 使用 GTID 模式
- 從 `gtid_slave_pos` 的位置開始複製

## Semi-Sync Replication

mariadb-operator 支援 semi-synchronous replication。

### 什麼是 Semi-Sync？

```
Async Replication:
  Primary: COMMIT → 回應 client → 傳給 replica
  問題: Primary crash 時可能丟失已 commit 的 transaction

Semi-Sync Replication:
  Primary: COMMIT → 等待至少 1 個 replica 確認 → 回應 client
  優點: 確保至少有 1 個 replica 有資料
```

### 設定方式

在 MariaDB CR 中：

```yaml
apiVersion: k8s.mariadb.com/v1alpha1
kind: MariaDB
spec:
  replication:
    enabled: true
    syncBinlog: true  # 啟用 semi-sync
```

### 相關 SQL

```sql
-- 檢查 semi-sync 狀態
SHOW STATUS LIKE 'Rpl_semi_sync%';

-- 重要指標
Rpl_semi_sync_master_status: ON/OFF
Rpl_semi_sync_master_clients: 2  -- 連接的 semi-sync replica 數量
Rpl_semi_sync_master_yes_tx:  100  -- 成功等到 ACK 的 transaction 數
Rpl_semi_sync_master_no_tx:   5    -- 降級為 async 的 transaction 數
```

## Replication User

Operator 會自動建立 replication user：

```go
// pkg/controller/mariadb/user.go
func (r *MariaDBReconciler) reconcileReplicationUser(ctx context.Context) error {
    // 建立 user
    client.CreateUser(ctx, "repl", "%", replPassword)

    // 授權
    client.Grant(ctx, "REPLICATION SLAVE", "*.*", "repl", "%")
}
```

對應 SQL：
```sql
CREATE USER 'repl'@'%' IDENTIFIED BY 'xxx';
GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%';
```

## Replication 狀態檢查

Operator 定期檢查 replication 狀態：

```go
// pkg/controller/replication/replication.go
func (r *ReplicationReconciler) getReplicationState(
    ctx context.Context,
    pod *corev1.Pod,
) (*ReplicationState, error) {
    client := r.getSqlClient(pod)

    // 取得 GTID 狀態
    gtidCurrentPos, _ := client.GetVariable(ctx, "gtid_current_pos")
    gtidSlavePos, _ := client.GetVariable(ctx, "gtid_slave_pos")

    // 取得 slave 狀態
    slaveStatus, _ := client.ShowSlaveStatus(ctx)

    return &ReplicationState{
        GtidCurrentPos:    gtidCurrentPos,
        GtidSlavePos:      gtidSlavePos,
        SlaveIORunning:    slaveStatus.SlaveIORunning,
        SlaveSQLRunning:   slaveStatus.SlaveSQLRunning,
        SecondsBehind:     slaveStatus.SecondsBehindMaster,
    }, nil
}
```

## 相關程式碼位置

| 功能 | 檔案 |
|------|------|
| Server ID 產生 | `pkg/controller/configmap/configmap.go` |
| ConfigurePrimary | `pkg/controller/galera/replication.go` |
| ConfigureReplica | `pkg/controller/galera/replication.go` |
| Replication User | `pkg/controller/mariadb/user.go` |
| SQL Client | `pkg/sql/sql.go` |
| 狀態檢查 | `pkg/controller/replication/replication.go` |

## 常見問題

### Q: 為什麼 replica 一直顯示 Seconds_Behind_Master = NULL？

可能原因：
1. IO thread 沒有連上 primary
2. SQL thread 停止了
3. 網路問題

檢查方式：
```sql
SHOW SLAVE STATUS\G
-- 檢查 Slave_IO_Running 和 Slave_SQL_Running
```

### Q: Replication lag 很大怎麼辦？

可能原因：
1. Primary 寫入量太大
2. Replica 硬體較慢
3. 網路延遲

解決方式：
1. 使用 parallel replication
2. 升級 replica 硬體
3. 減少 primary 寫入量
