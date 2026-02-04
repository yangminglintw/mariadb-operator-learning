# MariaDB Operator Switchover 流程

## 什麼是 Switchover？

Switchover 是**計畫性**的 primary 切換，用於：
- 維護舊 primary（升級、重啟）
- 負載平衡
- 測試 failover 機制

與 Failover 的差異：
| | Switchover | Failover |
|---|------------|----------|
| 觸發 | 手動 | 自動 |
| 舊 Primary | 仍然運作 | 已經掛掉 |
| 資料遺失風險 | 無 | 可能有 |

## 觸發條件

修改 MariaDB CR 的 `spec.replication.primary.podIndex`：

```yaml
apiVersion: k8s.mariadb.com/v1alpha1
kind: MariaDB
metadata:
  name: mariadb
spec:
  replication:
    enabled: true
    primary:
      podIndex: 1  # 改成想要的新 primary
```

Operator 偵測到 `spec.primary.podIndex != status.currentPrimaryPodIndex` 時啟動 switchover。

## 7 個 Phase 詳解

```go
// pkg/controller/replication/switchover.go
phases := []switchoverPhase{
    {name: "Lock old primary",           reconcile: r.lockOldPrimary},
    {name: "Wait for replicas to sync",  reconcile: r.waitForReplicasToSync},
    {name: "Configure new primary",      reconcile: r.configureNewPrimary},
    {name: "Configure replicas",         reconcile: r.configureReplicas},
    {name: "Unlock old primary",         reconcile: r.unlockOldPrimary},
    {name: "Configure old primary",      reconcile: r.configureOldPrimary},
    {name: "Kill user connections",      reconcile: r.killUserConnectionsOnOldPrimary},  // Issue #1509
}
```

### Phase 1: Lock old primary

```go
func (r *ReplicationReconciler) lockOldPrimary(ctx context.Context, ...) error {
    // 1. 設定 read_only = ON
    client.SetReadOnly(ctx, true)

    // 2. FLUSH TABLES WITH READ LOCK
    client.FlushTablesWithReadLock(ctx)

    return nil
}
```

**目的**：停止所有寫入，確保資料一致性。

**SQL 執行**：
```sql
SET GLOBAL read_only = ON;
FLUSH TABLES WITH READ LOCK;
```

### Phase 2: Wait for replicas to sync

```go
func (r *ReplicationReconciler) waitForReplicasToSync(ctx context.Context, ...) error {
    primaryGTID := getPrimaryGTID()

    for _, replica := range replicas {
        replicaGTID := getReplicaGTID(replica)

        // 等待 replica 追上 primary
        if replicaGTID < primaryGTID {
            return RequeueAfter(1 * time.Second)
        }
    }
    return nil
}
```

**目的**：確保所有 replica 都有最新資料。

**檢查方式**：
```sql
-- Primary
SHOW GLOBAL VARIABLES LIKE 'gtid_current_pos';
-- 結果: 0-10-1000

-- Replica (等待這個值達到 0-10-1000)
SHOW GLOBAL VARIABLES LIKE 'gtid_slave_pos';
```

### Phase 3: Configure new primary

```go
func (r *ReplicationReconciler) configureNewPrimary(ctx context.Context, ...) error {
    newPrimaryClient := getClient(newPrimaryPod)

    // 停止 replica
    newPrimaryClient.StopAllSlaves(ctx)

    // 清除 replica 設定
    newPrimaryClient.ResetAllSlaves(ctx)

    // 重置 binlog
    newPrimaryClient.ResetMaster(ctx)

    // 設定 read-write
    newPrimaryClient.SetReadOnly(ctx, false)

    return nil
}
```

**目的**：將新 primary 從 replica 模式轉換為 primary 模式。

**SQL 執行**：
```sql
STOP ALL SLAVES;
RESET SLAVE ALL;
RESET MASTER;
SET GLOBAL read_only = OFF;
```

### Phase 4: Configure replicas

```go
func (r *ReplicationReconciler) configureReplicas(ctx context.Context, ...) error {
    newPrimaryIP := getIP(newPrimaryPod)

    for _, replica := range replicas {
        if replica == newPrimary {
            continue  // 跳過新 primary
        }

        client := getClient(replica)

        // 指向新 primary
        client.ChangeMaster(ctx, &ChangeMasterOpts{
            Host:    newPrimaryIP,
            UseGTID: "slave_pos",
        })

        client.StartSlave(ctx)
    }
    return nil
}
```

**目的**：讓所有 replica 指向新 primary。

**SQL 執行**：
```sql
STOP SLAVE;
CHANGE MASTER TO
    MASTER_HOST='new-primary-ip',
    MASTER_USE_GTID=slave_pos;
START SLAVE;
```

### Phase 5: Unlock old primary

```go
func (r *ReplicationReconciler) unlockOldPrimary(ctx context.Context, ...) error {
    // 釋放 read lock
    client.UnlockTables(ctx)

    return nil
}
```

**目的**：釋放 Phase 1 取得的 lock。

**SQL 執行**：
```sql
UNLOCK TABLES;
```

### Phase 6: Configure old primary

```go
func (r *ReplicationReconciler) configureOldPrimary(ctx context.Context, ...) error {
    newPrimaryIP := getIP(newPrimaryPod)

    // 將舊 primary 設定為 replica
    client.ChangeMaster(ctx, &ChangeMasterOpts{
        Host:    newPrimaryIP,
        UseGTID: "slave_pos",
    })

    client.StartSlave(ctx)

    // 確保 read_only 維持 ON
    client.SetReadOnly(ctx, true)

    return nil
}
```

**目的**：舊 primary 變成 replica，指向新 primary。

### Phase 7: Kill user connections (Issue #1509)

```go
func (r *ReplicationReconciler) killUserConnectionsOnOldPrimary(ctx context.Context, ...) error {
    // 取得所有連線
    processList, _ := client.ShowProcessList(ctx)

    for _, proc := range processList {
        // 跳過系統連線
        if isSystemUser(proc.User) || isReplicationThread(proc.Command) {
            continue
        }

        // Kill user 連線
        client.Kill(ctx, proc.ID)
    }

    return nil
}
```

**目的**：強制斷開仍連接到舊 primary 的 client，讓他們重新連線到新 primary。

**過濾條件**：
- 排除 user: `root`, `mariadb-operator`, `system user`
- 排除 command: `Binlog Dump`, `Binlog Dump GTID`

## 時序圖

```
Timeline:
────────────────────────────────────────────────────────────────────────────────►

Old Primary (Pod-0):
├── Phase 1: LOCK (read_only=ON, FLUSH TABLES)
│   ├── ❌ 停止接受 writes
│   └── Clients 的 write 開始 timeout
├── Phase 5: UNLOCK TABLES
├── Phase 6: Configure as replica
│   ├── CHANGE MASTER TO new-primary
│   └── START SLAVE
└── Phase 7: KILL user connections
    └── ✂️ 強制斷開所有 user 連線

New Primary (Pod-1):
├── (等待 Phase 1-2 完成)
├── Phase 3: Configure as primary
│   ├── STOP ALL SLAVES
│   ├── RESET MASTER
│   └── read_only=OFF ✅
└── 開始接受 writes

Other Replicas (Pod-2):
├── Phase 2: 等待 sync
├── Phase 4: Reconfigure
│   ├── CHANGE MASTER TO new-primary
│   └── START SLAVE
└── 繼續從新 primary 複製

Service (selector 更新):
├── Loop N: 仍指向 Pod-0
└── Loop N+1: 更新指向 Pod-1
    └── 新連線會到 Pod-1
```

## 注意事項

### 1. Service 更新時機

Service selector 在**下一個 reconcile loop** 才更新：

```go
// Switchover reconcile 完成後
status.CurrentPrimaryPodIndex = newPrimaryIndex

// 下一個 reconcile loop
// Service reconciler 看到 status 更新，才更新 selector
```

這表示在 switchover 完成到 service 更新之間，有短暫時間：
- 舊 primary 已經 read-only
- Service 還指向舊 primary
- 新連線會失敗

### 2. 現有連線問題 (Issue #1509)

即使 service 更新了，**現有的 TCP 連線**仍然連接到舊 primary：
- Connection pool 的連線
- 長時間運作的 transaction
- Persistent connection

解決方式：Phase 7 kill 這些連線。

### 3. Switchover 過程中的錯誤處理

如果任何 phase 失敗：
1. Operator 會在下一次 reconcile 重試
2. 已完成的 phase 不會重做（透過狀態判斷）
3. 可能需要手動介入修復

## 相關程式碼位置

| 功能 | 檔案 | 行數 |
|------|------|------|
| Switchover 主流程 | `pkg/controller/replication/switchover.go` | - |
| Phase 定義 | `pkg/controller/replication/switchover.go` | phases 陣列 |
| SQL Client | `pkg/sql/sql.go` | - |
| Service 更新 | `pkg/controller/service/service_controller.go` | - |

## 手動測試 Switchover

```bash
# 1. 查看目前 primary
kubectl get mariadb mariadb -o jsonpath='{.status.currentPrimaryPodIndex}'

# 2. 修改 primary index
kubectl patch mariadb mariadb --type=merge -p '{"spec":{"replication":{"primary":{"podIndex":1}}}}'

# 3. 觀察 switchover 過程
kubectl logs -f deployment/mariadb-operator-controller-manager | grep -i switchover

# 4. 確認完成
kubectl get mariadb mariadb -o jsonpath='{.status.currentPrimaryPodIndex}'
```

## 常見問題

### Q: Switchover 卡在 "Wait for replicas to sync"？

可能原因：
1. Replica 有大量 lag
2. Replica 的 SQL thread 停止
3. 網路問題

解決方式：
```sql
-- 在 replica 上檢查
SHOW SLAVE STATUS\G
-- 看 Seconds_Behind_Master
```

### Q: Switchover 後 client 仍然連到舊 primary？

這就是 Issue #1509 要解決的問題。Phase 7 會 kill 這些連線。

### Q: 可以取消進行中的 switchover 嗎？

不建議。最好讓 switchover 完成，然後再做一次 switchover 切回去。
