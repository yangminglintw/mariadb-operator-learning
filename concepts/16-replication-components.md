# Replication 元件詳解

## Agent & Init Container

### Init Container 職責

```
cmd/init/replication.go

┌─────────────────────────────────────────────────────────────────┐
│              Init Container (replication mode)                  │
├─────────────────────────────────────────────────────────────────┤
│  1. 生成 0-replication.cnf                                      │
│     - log_bin, log_basename                                     │
│     - gtid_strict_mode                                          │
│     - rpl_semi_sync_* 設定                                      │
│     - server_id = serverIdBase + podIndex                       │
│     - sync_binlog                                               │
│                                                                 │
│  2. 等待 replica recovery 完成（如果正在恢復）                  │
│                                                                 │
│  3. 清理狀態檔案（首次設定時）                                  │
│     - master.info                                               │
│     - relay-log.info                                            │
└─────────────────────────────────────────────────────────────────┘
```

### Agent Sidecar 職責

```
cmd/agent/replication.go
pkg/agent/handler/replication/

┌─────────────────────────────────────────────────────────────────┐
│                Agent Sidecar (replication mode)                 │
├─────────────────────────────────────────────────────────────────┤
│  HTTP API Server (port 5555):                                   │
│  ├── /api/replication/gtid                                      │
│  │   └── 讀取 mariadb-operator.info 或 mariadb_backup_binlog_info│
│  │                                                              │
│  Probe Server (port 5556):                                      │
│  ├── /health/ready                                              │
│  │   └── 根據 Primary/Replica 執行不同健康檢查                  │
│  └── /health/live                                               │
│      └── 基本存活檢查                                           │
└─────────────────────────────────────────────────────────────────┘
```

### 狀態檔案

```
/var/lib/mysql/

├── mariadb-operator.info    # Operator 寫入的 GTID 資訊
│   └── Recovery 時用於設定 gtid_slave_pos
│
├── mariadb_backup_binlog_info   # 備份工具寫入
│   └── 含 binlog position 和 GTID
│
├── master.info              # MariaDB 原生，replica 設定
│   └── Init 時清理（首次設定）
│
└── relay-log.info           # MariaDB 原生，relay log 狀態
    └── Init 時清理（首次設定）
```

---

## 狀態追蹤與監控

### Role Detection

```
internal/controller/mariadb_controller_status.go:96-143

判斷邏輯：
┌─────────────────────────────────────────────────────────────────┐
│  For each Pod:                                                  │
│  1. IsReplicationReplica()?  ── 檢查 SHOW SLAVE STATUS         │
│  2. HasConnectedReplicas()?  ── 檢查是否有 replica 連接        │
│                                                                 │
│  Role 判定：                                                    │
│  ├── isReplica = true              → ReplicationRoleReplica    │
│  ├── hasConnectedReplicas = true   → ReplicationRolePrimary    │
│  └── 其他情況                      → ReplicationRoleUnknown    │
└─────────────────────────────────────────────────────────────────┘
```

### ReplicaStatus 監控

```
internal/controller/mariadb_controller_status.go:145-206

監控項目（每次 reconcile 更新）：
┌─────────────────────────────────────────────────────────────────┐
│  ReplicaStatusVars:                                             │
│  ├── LastIOErrno, LastIOError     # IO thread 錯誤              │
│  ├── LastSQLErrno, LastSQLError   # SQL thread 錯誤             │
│  ├── SlaveIORunning               # IO thread 狀態              │
│  ├── SlaveSQLRunning              # SQL thread 狀態             │
│  ├── SecondsBehindMaster          # Lag                         │
│  ├── GtidIOPos                    # IO thread GTID position     │
│  └── GtidCurrentPos               # SQL thread GTID position    │
│                                                                 │
│  ReplicaStatus:                                                 │
│  └── LastErrorTransitionTime      # 錯誤狀態轉換時間            │
│      └── 用於 Recovery 的 errorDurationThreshold 計算           │
└─────────────────────────────────────────────────────────────────┘
```

### Condition Types

```
Replication 相關 Conditions：
┌─────────────────────────────────────────────────────────────────┐
│  ConditionTypeReplicationConfigured                             │
│  └── Replication 首次設定完成                                   │
│                                                                 │
│  ConditionTypePrimarySwitched                                   │
│  ├── True  → Primary 切換完成                                   │
│  └── False → 正在進行 Switchover                                │
│                                                                 │
│  ConditionTypeReplicaRecovered                                  │
│  ├── True  → Replica 恢復完成                                   │
│  └── False → 正在恢復 Replica                                   │
└─────────────────────────────────────────────────────────────────┘
```

### Event Reasons

```
Replication 相關 Events：
┌─────────────────────────────────────────────────────────────────┐
│  Switchover:                                                    │
│  ├── ReasonReplicationPrimaryLock        # 鎖定 primary         │
│  ├── ReasonReplicationPrimaryReadonly    # 設定 read_only       │
│  ├── ReasonReplicationReplicaSync        # 等待 replica 同步    │
│  ├── ReasonReplicationReplicaSyncErr     # 同步錯誤             │
│  ├── ReasonReplicationPrimaryNew         # 配置新 primary       │
│  ├── ReasonReplicationPrimaryNewSync     # 等待新 primary 同步  │
│  ├── ReasonReplicationPrimaryNewSyncErr  # 新 primary 同步錯誤  │
│  ├── ReasonReplicationReplicaConn        # 連接 replicas        │
│  ├── ReasonReplicationPrimaryToReplica   # 舊 primary 降級      │
│  ├── ReasonPrimarySwitching              # 開始切換             │
│  ├── ReasonPrimarySwitched               # 切換完成             │
│  └── ReasonReplicationResetStaleSwitchover # 重置 stale 狀態    │
│                                                                 │
│  Failover:                                                      │
│  └── ReasonPrimarySwitching              # 開始自動 failover    │
│                                                                 │
│  Recovery:                                                      │
│  └── ReasonMariaDBReplicaRecoveryError   # Recovery 錯誤        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 實戰 Issues 案例

### Issue #1499: Switchover 卡住死循環

**問題描述**：Switchover 在 Phase 3 (Wait sync) 卡住，整個 cluster 癱瘓。

**根本原因**：GTID domain 不匹配 + 狀態機無法回退

```
Timeline:
1. Phase 1: Lock primary → primary 被鎖定，停止接受 writes
2. Phase 3: Wait sync timeout → GTID domain 0 vs domain 1 不匹配
3. 狀態機 retry → 無限重試 Phase 3
4. Primary 持續被鎖定 → 整個 cluster 無法寫入

問題：
- 狀態機只能前進，不能後退
- Sync timeout 後沒有解鎖 primary 的機制
- 沒有整體 timeout + 回退機制
```

**相關程式碼**：`pkg/controller/replication/switchover.go:188-254`

### Issue #1289: Failover 後舊 primary 角色不一致

**問題描述**：Failover 後出現兩個 Master，舊 primary 恢復後沒有自動降級為 replica。

**根本原因**：`ReconcilePodReady` 只清除 `CurrentPrimaryFailingSince`，沒有觸發 replication reconfigure

```go
// internal/controller/pod_replication_controller.go:48-71
func (r *PodReplicationController) ReconcilePodReady(ctx, pod, mariadb) error {
    // 只清除 failover timestamp
    if mariadb.Status.CurrentPrimaryFailingSince != nil {
        return r.patchStatus(ctx, mariadb, func(status) {
            status.CurrentPrimaryFailingSince = nil
        })
    }
    return nil
    // 缺少：檢查並重新配置為 replica 的邏輯
}
```

**影響**：
- 舊 primary 恢復後仍然認為自己是 Master
- 可能出現 split-brain（兩個可寫節點）
- 需要手動介入修復

### Issue #1046: Switchover 中 invalid connection

**問題描述**：Switchover 過程中 SQL 連線失效，導致中途失敗且無法恢復。

**根本原因**：`ReplicationClientSet` 快取連線沒有驗證機制

```go
// pkg/sql/sqlset.go
type ClientSet struct {
    clients map[int]*Client  // 快取連線，沒有 Ping 驗證
}

// 問題：
// 1. Pod 重啟後，快取的連線失效
// 2. 下次使用時才發現 invalid connection
// 3. Switchover 多步驟流程中斷
// 4. 狀態機卡在中間狀態，無法恢復
```

**影響**：
- Switchover 中途失敗
- `suspend: true` 也無法解決（switchover 狀態仍在）
- 需要手動修復 MariaDB 設定

---

## 關鍵程式碼路徑

| 功能 | 檔案 |
|------|------|
| Agent server | `cmd/agent/replication.go` |
| Init container | `cmd/init/replication.go` |
| Agent HTTP handlers | `pkg/agent/handler/replication/` |
| Agent client | `pkg/agent/client/replication.go` |
| 狀態更新 | `internal/controller/mariadb_controller_status.go` |
| Pod failover 觸發 | `internal/controller/pod_replication_controller.go` |
