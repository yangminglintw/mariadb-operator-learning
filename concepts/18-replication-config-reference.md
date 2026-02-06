# Replication Config 完整參照

## spec.replication 欄位總覽

```yaml
apiVersion: k8s.mariadb.com/v1alpha1
kind: MariaDB
spec:
  replication:
    enabled: true                    # 啟用 replication

    # Primary 設定
    primary:
      podIndex: 0                    # Primary pod index
      autoFailover: true             # 啟用自動 failover
      autoFailoverDelay: 30s         # Failover 延遲時間

    # Replica 設定
    replica:
      replPasswordSecretKeyRef:      # Replication 密碼
        name: mariadb-repl
        key: password
        generate: true
      gtid: CurrentPos               # GTID 模式
      connectionRetrySeconds: 60     # 重連間隔
      maxLagSeconds: 0               # 最大允許 lag
      syncTimeout: 10s               # 同步 timeout

      # Recovery 設定
      recovery:
        enabled: false
        errorDurationThreshold: 5m
      bootstrapFrom:
        physicalBackupTemplateRef:
          name: mariadb-backup
        restoreJob: {}

    # 全域設定
    gtidStrictMode: true             # GTID strict mode
    semiSyncEnabled: true            # Semi-sync replication
    semiSyncAckTimeout: 10s          # Semi-sync ACK timeout
    semiSyncWaitPoint: AfterCommit   # Semi-sync wait point
    syncBinlog: 1                    # Binlog sync 頻率
    serverIdBase: 10                 # Server ID 基數
    standaloneProbes: false          # 使用非 HA probes

    # Data plane 設定
    initContainer:
      image: mariadb-operator:latest
    agent:
      image: mariadb-operator:latest
      port: 5555
      probePort: 5556
```

## Primary 設定

### primary.podIndex

| 屬性 | 值 |
|------|-----|
| 類型 | `*int` |
| 預設值 | `0` |
| 用途 | 指定 primary pod 的 StatefulSet index |

**觸發 Switchover**：
```yaml
# 修改此欄位會觸發 switchover
spec:
  replication:
    primary:
      podIndex: 1  # 從 0 改成 1
```

### primary.autoFailover

| 屬性 | 值 |
|------|-----|
| 類型 | `*bool` |
| 預設值 | `true` |
| 用途 | 是否自動進行 failover |

**設定為 false**：
- Primary 故障時不會自動切換
- 需要手動更新 `podIndex` 來 failover

### primary.autoFailoverDelay

| 屬性 | 值 |
|------|-----|
| 類型 | `*metav1.Duration` |
| 預設值 | `0s`（無延遲） |
| 用途 | Primary 故障後等待多久才 failover |

**實作邏輯**：
```go
// internal/controller/pod_replication_controller.go:103-112
autoFailoverDelay := mariadb.GetAutomaticFailoverDelay()
if autoFailoverDelay > 0 {
    failoverTime := mariadb.Status.CurrentPrimaryFailingSince.Add(autoFailoverDelay)
    if failoverTime.After(now) {
        return ErrDelayAutomaticFailover  // 延遲 failover
    }
}
```

## Replica 設定

### replica.replPasswordSecretKeyRef

| 屬性 | 值 |
|------|-----|
| 類型 | `*GeneratedSecretKeyRef` |
| 預設值 | 自動生成 `{mariadb-name}-repl` |
| 用途 | Replication 用戶密碼 |

**自動生成**：
```yaml
spec:
  replication:
    replica:
      replPasswordSecretKeyRef:
        name: mariadb-repl
        key: password
        generate: true  # 如果 Secret 不存在則自動生成
```

### replica.gtid

| 屬性 | 值 |
|------|-----|
| 類型 | `*Gtid` |
| 預設值 | `CurrentPos` |
| 可選值 | `CurrentPos`, `SlavePos` |
| 用途 | CHANGE MASTER TO 使用的 GTID 模式 |

**差異**：
- `CurrentPos`：使用 `gtid_current_pos`（union of binlog_pos 和 slave_pos）
- `SlavePos`：僅使用 `gtid_slave_pos`

**SQL 對應**：
```sql
-- CurrentPos
CHANGE MASTER TO MASTER_USE_GTID=current_pos;

-- SlavePos
CHANGE MASTER TO MASTER_USE_GTID=slave_pos;
```

詳見 07-gtid-concepts.md

### replica.connectionRetrySeconds

| 屬性 | 值 |
|------|-----|
| 類型 | `*int` |
| 預設值 | 不設定（使用 MariaDB 預設 60s） |
| 用途 | Replica IO thread 重連間隔 |

**SQL 對應**：
```sql
CHANGE MASTER TO MASTER_CONNECT_RETRY=60;
```

### replica.maxLagSeconds

| 屬性 | 值 |
|------|-----|
| 類型 | `*int` |
| 預設值 | `0`（不允許任何 lag） |
| 用途 | 最大允許 replication lag |

**影響**：
1. **Failover 候選人篩選**：
   - Lag 超過閾值的 replica 不會被選為新 primary
2. **Switchover 阻斷**：
   - 有 replica lag 超過閾值時 switchover 會等待
3. **Secondary Service**：
   - Lag 超過閾值的 replica 會從 endpoints 移除

**注意**：MaxScale 不使用此欄位，需設定 router 的 `max_replication_lag`。

### replica.syncTimeout

| 屬性 | 值 |
|------|-----|
| 類型 | `*metav1.Duration` |
| 預設值 | `10s` |
| 用途 | Switchover/Failover 時的同步等待 timeout |

**使用場景**：
1. **Switchover Phase 3**：等待所有 replica 同步到 primary GTID
2. **Failover**：等待新 primary 處理完 relay log

**SQL 對應**：
```sql
-- 內部使用 MASTER_GTID_WAIT 等待
SELECT MASTER_GTID_WAIT('0-10-1000', 10);  -- timeout 10 秒
```

### replica.recovery

| 屬性 | 值 |
|------|-----|
| 類型 | `*ReplicaRecovery` |
| 預設值 | `{enabled: false}` |
| 用途 | 自動恢復錯誤的 replica |

```yaml
spec:
  replication:
    replica:
      recovery:
        enabled: true
        errorDurationThreshold: 5m  # 非 Error 1236 的等待時間
```

**必須同時設定 `bootstrapFrom`**，否則 webhook validation 會失敗。

詳見 17-replica-recovery.md

### replica.bootstrapFrom

| 屬性 | 值 |
|------|-----|
| 類型 | `*ReplicaBootstrapFrom` |
| 預設值 | `nil` |
| 用途 | Recovery 和 scale-out 的資料來源 |

```yaml
spec:
  replication:
    replica:
      bootstrapFrom:
        physicalBackupTemplateRef:
          name: mariadb-backup       # PhysicalBackup CR 名稱
        restoreJob:                  # 還原 Job 設定
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
```

## 全域設定

### gtidStrictMode

| 屬性 | 值 |
|------|-----|
| 類型 | `*bool` |
| 預設值 | `true` |
| 用途 | 啟用 GTID strict mode |

**對應設定**：
```ini
# 0-replication.cnf
gtid_strict_mode = ON
```

**效果**：
- 禁止非 GTID 的 binlog events
- 更嚴格的 GTID 一致性檢查

### semiSyncEnabled

| 屬性 | 值 |
|------|-----|
| 類型 | `*bool` |
| 預設值 | `true` |
| 用途 | 啟用 semi-synchronous replication |

**對應設定**：
```ini
# 0-replication.cnf
rpl_semi_sync_master_enabled = ON
rpl_semi_sync_slave_enabled = ON
```

詳見 12-semi-sync-and-error-1236.md

### semiSyncAckTimeout

| 屬性 | 值 |
|------|-----|
| 類型 | `*metav1.Duration` |
| 預設值 | MariaDB 預設（10s） |
| 用途 | 等待 replica ACK 的 timeout |

**對應設定**：
```ini
# 0-replication.cnf
rpl_semi_sync_master_timeout = 10000  # 毫秒
```

**超時行為**：
- 超時後降級為 async replication
- 記錄在 `Rpl_semi_sync_master_no_tx` counter

### semiSyncWaitPoint

| 屬性 | 值 |
|------|-----|
| 類型 | `*WaitPoint` |
| 預設值 | `AfterCommit` |
| 可選值 | `AfterSync`, `AfterCommit` |
| 用途 | Semi-sync 等待點 |

**差異**：

| | AfterSync | AfterCommit |
|---|-----------|-------------|
| 等待時機 | Commit 前等 ACK | Commit 後等 ACK |
| 一致性 | 更強 | 較弱 |
| 效能 | 較慢 | 較快 |
| Crash 安全 | 更安全 | 可能有 phantom read |

詳見 12-semi-sync-and-error-1236.md

### syncBinlog

| 屬性 | 值 |
|------|-----|
| 類型 | `*int` |
| 預設值 | 不設定（使用 MariaDB 預設） |
| 用途 | Binlog sync 頻率 |

**對應設定**：
```ini
# 0-replication.cnf
sync_binlog = 1
```

**常用值**：
- `0`：不主動 sync（依賴 OS）
- `1`：每個 transaction sync（最安全）
- `N`：每 N 個 transaction sync

### serverIdBase

| 屬性 | 值 |
|------|-----|
| 類型 | `*int` |
| 預設值 | `10` |
| 用途 | Server ID 計算基數 |

**計算公式**：
```
server_id = serverIdBase + podIndex
```

**範例**（serverIdBase=10）：
- Pod-0 → server_id = 10
- Pod-1 → server_id = 11
- Pod-2 → server_id = 12

**多 cluster 設定**：
```yaml
# Cluster A
spec:
  replication:
    serverIdBase: 10  # server_id: 10, 11, 12

# Cluster B（避免衝突）
spec:
  replication:
    serverIdBase: 100  # server_id: 100, 101, 102
```

### standaloneProbes

| 屬性 | 值 |
|------|-----|
| 類型 | `*bool` |
| 預設值 | `false` |
| 用途 | 使用非 HA 的 startup/liveness probes |

**設定為 true 時**：
- 使用簡單的 TCP probe
- 不檢查 replication 狀態
- 適用於 debug 或特殊場景

## Data Plane 設定

### initContainer

| 屬性 | 值 |
|------|-----|
| 類型 | `InitContainer` |
| 預設值 | 使用 operator image |
| 用途 | Init container 設定 |

```yaml
spec:
  replication:
    initContainer:
      image: mariadb-operator:latest
      imagePullPolicy: IfNotPresent
      resources:
        requests:
          cpu: 10m
          memory: 32Mi
```

**職責**：
- 生成 `0-replication.cnf`
- 清理狀態檔案（首次設定）
- 等待 recovery 完成

### agent

| 屬性 | 值 |
|------|-----|
| 類型 | `Agent` |
| 預設值 | 使用 operator image |
| 用途 | Agent sidecar 設定 |

```yaml
spec:
  replication:
    agent:
      image: mariadb-operator:latest
      port: 5555
      probePort: 5556
      basicAuth:
        enabled: true
        username: mariadb-operator
        passwordSecretKeyRef:
          name: mariadb-agent
          key: password
```

**職責**：
- 提供 GTID API（/api/replication/gtid）
- 提供健康探針（/health/ready, /health/live）

## 關鍵程式碼路徑

| 功能 | 檔案 |
|------|------|
| 所有欄位定義 | `api/v1alpha1/mariadb_replication_types.go` |
| 預設值設定 | `api/v1alpha1/mariadb_replication_types.go` (SetDefaults) |
| Validation | `api/v1alpha1/mariadb_replication_types.go` (Validate) |
| Init container 生成 cnf | `pkg/controller/replication/config.go:235-313` |
