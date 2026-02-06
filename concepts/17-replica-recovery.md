# Replica Recovery 自動恢復機制

## 什麼是 Replica Recovery？

Replica Recovery 是 mariadb-operator 的自動修復機制，當 replica 的 replication 進入錯誤狀態時，自動從備份恢復該 replica。

### 與 Failover 的差異

| | Replica Recovery | Failover |
|---|------------------|----------|
| 處理對象 | Replica 故障 | Primary 故障 |
| 觸發條件 | IO/SQL thread 錯誤 | Primary Pod NotReady |
| 修復方式 | 從備份重建 replica | 提升 replica 為 primary |
| 資料處理 | 刪除 replica 資料，從備份還原 | 保留 replica 資料 |
| 風險 | 低（只影響單一 replica） | 中（可能有資料遺失）|

## 觸發條件

### shouldReconcileReplicaRecovery() 前置檢查

```go
// internal/controller/mariadb_controller_replica_recovery.go:35-44

func shouldReconcileReplicaRecovery(mdb *MariaDB) bool {
    // 必須啟用 replication 且已完成首次設定
    if !mdb.IsReplicationEnabled() || !mdb.HasConfiguredReplication() {
        return false
    }

    // 以下狀態不進行 recovery
    if mdb.IsSwitchingPrimary() ||           // 正在 switchover
       mdb.IsReplicationSwitchoverRequired() || // 需要 switchover
       mdb.IsInitializing() ||                // 正在初始化
       mdb.IsScalingOut() ||                  // 正在擴展
       mdb.IsRestoringBackup() ||             // 正在還原
       mdb.IsResizingStorage() ||             // 正在調整儲存
       mdb.IsUpdating() {                     // 正在更新
        return false
    }
    return true
}
```

### 可恢復的 IO 錯誤碼

```go
// internal/controller/mariadb_controller_replica_recovery.go:29-33

var recoverableIOErrorCodes = []int{
    // Error 1236: Got fatal error from master when reading data from binary log.
    // 當 replica 要求的 GTID position 不在 primary 的 binlog 中
    1236,
}
```

**Error 1236 典型場景**：
1. Primary 執行了 `RESET MASTER`，清除 binlog
2. Replica 重啟後找不到對應的 GTID position
3. Binlog 檔案因為過期被自動清理

### errorDurationThreshold

對於非 Error 1236 的其他錯誤，會等待一段時間後才觸發 recovery：

```go
// internal/controller/mariadb_controller_replica_recovery.go:459-484

func isRecoverableError(mdb *MariaDB, status ReplicaStatus, codes []int, logger) bool {
    // 1. Error 1236 立即觸發
    for _, code := range recoverableIOErrorCodes {
        if status.LastIOErrno != nil && *status.LastIOErrno == code {
            return true
        }
    }

    // 2. 其他錯誤檢查持續時間
    if (lastIOErrno != 0 || lastSQLErrno != 0) && !status.LastErrorTransitionTime.IsZero() {
        errThreshold := ptr.Deref(recovery.ErrorDurationThreshold, 5 * time.Minute)
        age := time.Since(status.LastErrorTransitionTime.Time)

        if age > errThreshold.Duration {
            return true  // 錯誤持續超過閾值，觸發 recovery
        }
    }
    return false
}
```

### ReplicaStatusVars 錯誤偵測

狀態監控來源：`internal/controller/mariadb_controller_status.go:145-206`

```
監控每個 Replica 的：
├── LastIOErrno      # IO thread 錯誤碼（如 1236）
├── LastIOError      # IO thread 錯誤訊息
├── LastSQLErrno     # SQL thread 錯誤碼
├── LastSQLError     # SQL thread 錯誤訊息
└── LastErrorTransitionTime  # 錯誤狀態開始時間
```

## Recovery 設定

### API 定義

```yaml
# api/v1alpha1/mariadb_replication_types.go:129-143, 183-194

apiVersion: k8s.mariadb.com/v1alpha1
kind: MariaDB
spec:
  replication:
    enabled: true
    replica:
      # Recovery 設定
      recovery:
        enabled: true                           # 啟用自動恢復
        errorDurationThreshold: 5m              # 非 1236 錯誤等待時間

      # ★ 必須設定 bootstrapFrom，否則 recovery 無法運作
      bootstrapFrom:
        physicalBackupTemplateRef:
          name: mariadb-backup                  # PhysicalBackup CR 模板
        restoreJob:                             # 還原 Job 設定（可選）
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
```

### 啟用條件

```go
// api/v1alpha1/mariadb_replication_types.go:211-223

func (r *ReplicaReplication) Validate() error {
    recoveryEnabled := ptr.Deref(r.ReplicaRecovery, ReplicaRecovery{}).Enabled
    if recoveryEnabled && r.ReplicaBootstrapFrom == nil {
        return errors.New("'bootstrapFrom' must be set when 'recovery` is enabled")
    }
    return nil
}
```

## Recovery 流程

### 流程總覽

```
internal/controller/mariadb_controller_replica_recovery.go:46-97

┌─────────────────────────────────────────────────────────────────────┐
│                    reconcileReplicaRecovery()                       │
└─────────────────────────────────────────────────────────────────────┘
                               │
                ┌──────────────┴──────────────┐
                ▼                             ▼
    shouldReconcileReplicaRecovery()?    IsReplicaRecoveryEnabled()?
           No → return                         No → reset & return
                                               Yes → continue
                               │
                               ▼
              ┌────────────────────────────────┐
              │ getReplicasToRecover(mdb)      │
              │ 找出需要恢復的 replicas        │
              └────────────────────────────────┘
                               │
              ┌────────────────┴────────────────┐
              ▼                                 ▼
    len(replicas) == 0               len(replicas) > 0
         │                                      │
         ▼                                      ▼
    setReplicaRecoveredAndCleanup()     For each replica:
         │                                      │
         ▼                              ┌───────┴───────┐
    return (cleanup backup, jobs)       ▼               ▼
                                  VolumeSnapshot?   Job-based?
                                        │               │
                                        ▼               ▼
                          reconcileSnapshotReplicaRecovery()
                                        │
                          reconcileJobReplicaRecovery()
                                        │
                                        ▼
                        ensureReplicationConfiguredInPod()
                                        │
                                        ▼
                        ensureReplicaRecovered()
```

### 詳細步驟

#### Step 1: 偵測 replica 錯誤

```go
// 每次 reconcile 時檢查 replica status
// internal/controller/mariadb_controller_status.go:145-206

// 狀態追蹤：
status.Replication.Replicas[podName] = ReplicaStatus{
    LastIOErrno:             1236,  // Error 1236
    LastIOError:             "Got fatal error...",
    LastErrorTransitionTime: now,   // 錯誤開始時間
}
```

#### Step 2: 建立 PhysicalBackup

```go
// internal/controller/mariadb_controller_replica_recovery.go:82-86

physicalBackupKey := mariadb.PhysicalBackupReplicaRecoveryKey()
if result, err := r.reconcileReplicaPhysicalBackup(ctx, physicalBackupKey, mariadb, logger); ... {
    return result, err
}
```

備份來源選擇：
1. 健康的 replica（優先）
2. Primary（如果沒有健康的 replica）

#### Step 3: 還原備份到故障 replica

**Job-based Recovery**（非 VolumeSnapshot）：

```go
// internal/controller/mariadb_controller_replica_recovery.go:161-203

func (r *MariaDBReconciler) reconcileJobReplicaRecovery(ctx, replica, backup, mdb, logger) {
    // 1. 標記正在恢復的 replica
    mariadb.SetReplicaToRecover(&replica)

    // 2. 確保 Pod 在 initializing 狀態
    //    （刪除並等待新 Pod 建立）
    r.ensurePodInitializing(ctx, podKey, logger)

    // 3. 建立並等待還原 Job 完成
    r.reconcileAndWaitForRecoveryJob(ctx, backup, mdb, podKey, logger)

    // 4. 清除恢復標記
    mariadb.SetReplicaToRecover(nil)
}
```

**Snapshot-based Recovery**（VolumeSnapshot）：

```go
// internal/controller/mariadb_controller_replica_recovery.go:205-289

func (r *MariaDBReconciler) reconcileSnapshotReplicaRecovery(ctx, replica, backup, mdb, snapshotKey, logger) {
    // 1. 等待 VolumeSnapshot 就緒
    r.waitForReadyVolumeSnapshot(ctx, snapshotKey, logger)

    // 2. 刪除 StatefulSet（保留 orphan Pods）
    r.deleteStatefulSetLeavingOrphanPods(ctx, mariadb)

    // 3. 刪除 Pod
    r.Delete(ctx, &pod)

    // 4. 刪除 PVC
    r.Delete(ctx, &pvc)

    // 5. 從 snapshot 建立新 PVC
    r.reconcilePVC(ctx, mariadb, pvcKey, builder.WithVolumeSnapshotDataSource(snapshotKey.Name))

    // 6. 重建 StatefulSet（會自動建立新 Pod）
    r.reconcileStatefulSet(ctx, mariadb)
}
```

#### Step 4: 設定 GTID slave position

```go
// pkg/controller/replication/controller.go:226-276

// Recovery 後呼叫 ReconcileReplicationInPod 時帶入 GTID
r.ReconcileReplicationInPod(ctx, req, podIndex, logger,
    WithForceReplicaConfiguration(true),
    WithVolumeSnapshotKey(snapshotKey),  // or 從 agent 取得 GTID
)

// GTID 來源：
// 1. VolumeSnapshot annotation: k8s.mariadb.com/gtid
// 2. Agent API: GET /api/replication/gtid
//    └── 讀取 mariadb-operator.info 或 mariadb_backup_binlog_info
```

#### Step 5: 重新啟動 replication

```go
// pkg/controller/replication/config.go:95-127

func (r *ReplicationConfigClient) ConfigureReplica(ctx, mdb, client, primaryPodIndex, opts...) error {
    // RESET MASTER
    client.ResetMaster(ctx)

    // STOP ALL SLAVES
    client.StopAllSlaves(ctx)

    // SET GLOBAL gtid_slave_pos = 'x-y-z'  (從備份取得)
    if opts.GtidSlavePos != nil {
        client.SetGtidSlavePos(ctx, *opts.GtidSlavePos)
    }

    // SET GLOBAL read_only = ON
    client.EnableReadOnly(ctx)

    // CHANGE MASTER TO ...
    r.changeMaster(ctx, mdb, client, primaryPodIndex, opts.ChangeMasterOpts...)

    // START SLAVE
    client.StartSlave(ctx)

    return nil
}
```

#### Step 6: 清理

```go
// internal/controller/mariadb_controller_replica_recovery.go:397-416

func (r *MariaDBReconciler) setReplicaRecoveredAndCleanup(ctx, mariadb) error {
    // 1. 設定 ReplicaRecovered condition
    condition.SetReplicaRecovered(status)

    // 2. 清除恢復中的 replica 標記
    mariadb.SetReplicaToRecover(nil)

    // 3. 刪除臨時建立的 PhysicalBackup
    r.cleanupPhysicalBackup(ctx, mariadb.PhysicalBackupReplicaRecoveryKey())

    // 4. 清理初始化 Jobs
    r.cleanupInitJobs(ctx, mariadb)

    return nil
}
```

## Init Container 在 Recovery 中的角色

```
cmd/init/replication.go:95-129

┌─────────────────────────────────────────────────────────────────────┐
│               Init Container Recovery 流程                          │
└─────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
              ┌────────────────────────────────┐
              │ IsReplicaBeingRecovered()?     │
              │ 檢查此 Pod 是否正在被恢復      │
              └────────────────────────────────┘
                               │
              ┌────────────────┴────────────────┐
              ▼                                 ▼
           Yes                               No
              │                                 │
              ▼                                 ▼
    waitForReplicaRecovery()          cleanupReplicaState()
    每秒檢查直到恢復完成                （僅首次設定時）
              │                                 │
              ▼                                 ▼
        繼續啟動                         刪除 master.info
                                        刪除 relay-log.info
```

### 狀態檔案清理

```go
// cmd/init/replication.go:117-129

func cleanupReplicaState(fm *filemanager.FileManager, mdb *MariaDB, podIndex int) error {
    // 只在首次設定時清理
    if mdb.HasConfiguredReplication() || mdb.IsSwitchingPrimary() {
        return nil
    }

    // 清理 MariaDB 原生的 replica 狀態檔案
    for _, file := range []string{
        replicationresources.MasterInfoFileName,   // master.info
        replicationresources.RelayLogFileName,     // relay-log.info
    } {
        cleanupStateFile(fm, file)
    }
    return nil
}
```

## Relay Log Event 偵測

Recovery 前需要確保 relay log 已完全處理：

```go
// pkg/controller/replication/relay_log.go:13-53

func HasRelayLogEvents(status *ReplicaStatusVars, gtidDomainId uint32, logger) (bool, error) {
    // 比較 GtidIOPos 和 GtidCurrentPos
    //
    // GtidIOPos:      IO thread 從 primary 接收到的最新 GTID
    // GtidCurrentPos: SQL thread 已執行的 GTID
    //
    // 如果 GtidIOPos > GtidCurrentPos，表示還有未執行的 events

    gtidIOPos, _ := replication.ParseGtid(*status.GtidIOPos, gtidDomainId, logger)
    gtidCurrentPos, _ := replication.ParseGtid(*status.GtidCurrentPos, gtidDomainId, logger)

    if gtidIOPos.Equal(gtidCurrentPos) {
        return false, nil  // 已同步
    }

    greaterThan, _ := gtidIOPos.GreaterThan(gtidCurrentPos)
    if greaterThan {
        logger.Info("Detected events in relay log. Skipping...")
        return true, nil  // 還有未處理的 events
    }

    // GtidCurrentPos > GtidIOPos 是異常狀態
    logger.Info("GTID SQL position ahead of IO (unexpected state)")
    return false, nil
}
```

## 實戰 Issues 案例

### Issue #1235: Error 1236 GTID not in binlog

**問題描述**：執行 `RESET MASTER` 後，replica 報錯 Error 1236。

**根本原因**：
```
Primary: RESET MASTER
         └── 清除 binlog，gtid_binlog_pos 重置

Replica: 仍然記得舊的 gtid_slave_pos
         └── 請求舊的 GTID position → Error 1236
```

**Recovery 如何解決**：
1. 偵測到 Error 1236
2. 從健康節點建立備份
3. 還原到故障 replica
4. 使用備份中的 GTID 設定 `gtid_slave_pos`
5. 重新 CHANGE MASTER TO

**相關筆記**：12-semi-sync-and-error-1236.md

### Issue #1508: Replica recovery 不觸發

**問題描述**：刪除 replica PVC 後重建，replica 認為自己是 master，recovery 未觸發。

**根本原因**：
```
1. 刪除 PVC → 資料清空
2. Pod 重啟 → MariaDB 啟動時沒有 slave 設定
3. MariaDB 預設為 standalone（像 master）
4. Operator 檢查 IsReplicationReplica() → false
5. hasConnectedReplicas() → false
6. Role = Unknown（不是錯誤狀態）
7. Recovery 不觸發（沒有 IO/SQL error）
```

**缺失的邏輯**：
- Recovery 只檢查 IO/SQL thread 錯誤
- 沒有處理「replica 完全失去 slave 設定」的情況

### Issue #1488: PhysicalBackup recovery 模板 bug

**問題描述**：Recovery 建立的 PhysicalBackup 因為缺少 `schedule.cron` 導致 webhook validation 失敗。

**根本原因**：
```go
// Bootstrap 建立 PhysicalBackup 時
physicalBackup := &PhysicalBackup{
    Spec: PhysicalBackupSpec{
        // 從 template 複製
        Storage: template.Spec.Storage,
        // 缺少 Schedule.Cron → webhook 驗證失敗
    },
}

// Webhook validation (internal/webhook/v1alpha1/physicalbackup_webhook.go)
if pb.Spec.Schedule.Cron == "" {
    return errors.New("schedule.cron is required")
}
```

**修復**：Bootstrap 建立的 PhysicalBackup 需要明確設定為一次性（非排程）模式。

## 與 Error 1236 的連結

Error 1236 是最常觸發 Replica Recovery 的錯誤：

```
觸發場景：
┌─────────────────────────────────────────────────────────────────────┐
│  Primary: RESET MASTER                                              │
│           ↓                                                         │
│  Binlog 清空，gtid_binlog_pos 重置                                  │
│           ↓                                                         │
│  Replica: 請求舊 GTID position → Error 1236                        │
│           ↓                                                         │
│  Recovery: 偵測到 1236，立即觸發                                    │
│           ↓                                                         │
│  建立備份 → 還原 → 設定新 GTID → 重新連接                           │
└─────────────────────────────────────────────────────────────────────┘
```

詳細分析請參照 12-semi-sync-and-error-1236.md

## 關鍵程式碼路徑

| 功能 | 檔案 |
|------|------|
| 核心恢復邏輯 | `internal/controller/mariadb_controller_replica_recovery.go` |
| Init phase | `internal/controller/mariadb_controller_init.go` |
| Init container 實作 | `cmd/init/replication.go` |
| Relay log 偵測 | `pkg/controller/replication/relay_log.go` |
| 常數和 binlog 檔名 | `pkg/replication/replication.go` |
| Recovery conditions | `pkg/condition/replica_recovered.go` |
| ReplicaRecovery spec | `api/v1alpha1/mariadb_replication_types.go` |
