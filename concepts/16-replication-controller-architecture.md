# Replication 控制器架構總覽

## 概述

本文檔說明 mariadb-operator 中 Replication 系統的整體架構，包括控制器結構、資料流、以及各元件如何協作。

## Reconciliation 全景圖

MariaDB Controller 的 reconcile 流程分為 21 個 phases，Replication 是 Phase 15：

```
internal/controller/mariadb_controller.go:131-216

Phase 1:  Spec           - 設定預設值
Phase 2:  Status         - 更新狀態
Phase 3:  Suspend        - 檢查暫停狀態
Phase 4:  Secret         - 密碼管理
Phase 5:  ConfigMap      - 配置檔案
Phase 6:  TLS            - 憑證管理
Phase 7:  RBAC           - 權限設定
Phase 8:  Init           - 初始化 Job
Phase 9:  Scale out      - 擴展 replicas
Phase 10: Replica recovery - Replica 自動恢復
Phase 11: Storage        - PVC 管理
Phase 12: StatefulSet    - 核心資源
Phase 13: PodDisruptionBudget
Phase 14: Service        - 服務暴露
Phase 15: Replication    - ★ 本文重點
Phase 16: Labels         - Pod role 標籤
Phase 17: Galera         - Galera cluster
Phase 18: Restore        - 備份還原
Phase 19: SQL            - Database/User/Grant
Phase 20: Metrics        - 監控
Phase 21: Connection     - Connection CR
```

### Phase 依賴關係

```
StatefulSet (Phase 12)
       │
       ▼
   Service (Phase 14)
       │
       ▼
  Replication (Phase 15)  ◄── 需要 Pod Ready + Service 可連
       │
       ▼
   Labels (Phase 16)       ◄── 需要知道 primary/replica
```

## ReplicationReconciler 結構

```
pkg/controller/replication/controller.go:51-61

┌─────────────────────────────────────────────────────────────────────┐
│                     ReplicationReconciler                           │
├─────────────────────────────────────────────────────────────────────┤
│  client.Client           - Kubernetes API 存取                      │
│  recorder                - Event 記錄器                             │
│  builder                 - 資源建構器                               │
│  env                     - Operator 環境變數                        │
│  replConfigClient        - ★ SQL 設定 client（ConfigurePrimary/Replica）│
│  refResolver             - Secret/ConfigMap 引用解析                │
│  secretReconciler        - Secret 管理                              │
│  configMapReconciler     - ConfigMap 管理                           │
│  serviceReconciler       - Service 管理                             │
└─────────────────────────────────────────────────────────────────────┘
```

### Reconcile() 入口流程

```go
// pkg/controller/replication/controller.go:129-149

func (r *ReplicationReconciler) Reconcile(ctx, mdb) (ctrl.Result, error) {
    // 1. 非 replication 模式直接返回
    if !mdb.IsReplicationEnabled() {
        return ctrl.Result{}, nil
    }

    // 2. 建立 ReconcileRequest（含 SQL 連線集）
    req, err := r.NewReconcileRequest(ctx, mdb)
    defer req.Close()

    // 3. 判斷要做 switchover 還是一般 reconcile
    if mdb.IsReplicationSwitchoverRequired() {
        return ctrl.Result{}, r.reconcileSwitchover(ctx, req, logger)
    }

    // 4. 一般 replication reconcile
    if result, err := r.reconcileReplication(ctx, req, logger); !result.IsZero() || err != nil {
        return result, err
    }

    // 5. 檢查是否需要 switchover
    return ctrl.Result{}, r.reconcileSwitchover(ctx, req, logger)
}
```

### reconcileReplication() 核心邏輯

```go
// pkg/controller/replication/controller.go:151-168

func (r *ReplicationReconciler) reconcileReplication(ctx, req, logger) {
    // 1. 前置檢查
    if result, err := r.shouldReconcileReplication(ctx, req, logger); !result.IsZero() || err != nil {
        return result, err
    }

    // 2. 逐 Pod 配置（先 Primary，再 Replicas）
    for _, i := range r.replicationPodIndexes(req) {  // [primaryIndex, 0, 1, 2...]
        if result, err := r.ReconcileReplicationInPod(ctx, req, i, logger); !result.IsZero() || err != nil {
            return result, err
        }
    }

    // 3. 設定 ReplicationConfigured condition
    if !req.mariadb.HasConfiguredReplication() {
        r.patchStatus(ctx, req.mariadb, func(status) {
            conditions.SetReplicationConfigured(status)
        })
    }

    return ctrl.Result{}, nil
}
```

### ReconcileReplicationInPod() 流程

```
pkg/controller/replication/controller.go:226-276

┌──────────────────────────────────────────────────────────────────┐
│              ReconcileReplicationInPod(podIndex)                 │
└──────────────────────────────────────────────────────────────────┘
                              │
                ┌─────────────┴─────────────┐
                ▼                           ▼
    podIndex == primaryIndex?          podIndex != primaryIndex
                │                           │
                ▼                           ▼
    ┌───────────────────────┐     ┌───────────────────────┐
    │ replRoles[pod] ==     │     │ replRoles[pod] ==     │
    │ Primary?              │     │ Replica?              │
    │   Yes → return        │     │   Yes → return        │
    │   No  → ▼             │     │   No  → ▼             │
    └───────────────────────┘     └───────────────────────┘
                │                           │
                ▼                           ▼
    ┌───────────────────────┐     ┌───────────────────────┐
    │ ConfigurePrimary()    │     │ ConfigureReplica()    │
    │ - STOP ALL SLAVES     │     │ - RESET MASTER        │
    │ - RESET SLAVE ALL     │     │ - STOP ALL SLAVES     │
    │ - read_only = OFF     │     │ - SET gtid_slave_pos  │
    │ - 建立 repl 用戶      │     │ - read_only = ON      │
    └───────────────────────┘     │ - CHANGE MASTER TO    │
                                  │ - START SLAVE         │
                                  └───────────────────────┘
```

## 連線管理架構

### ReplicationClientSet

```
pkg/controller/replication/clientset.go

┌─────────────────────────────────────────────────────────────────┐
│                    ReplicationClientSet                         │
├─────────────────────────────────────────────────────────────────┤
│  *sqlClient.ClientSet                                           │
│    └── map[int]*sql.Client  // 快取，key = podIndex            │
├─────────────────────────────────────────────────────────────────┤
│  Methods:                                                       │
│  ├── clientForIndex(podIndex)     → *sql.Client                │
│  ├── currentPrimaryClient()       → *sql.Client                │
│  └── newPrimaryClient()           → *sql.Client                │
└─────────────────────────────────────────────────────────────────┘

注意：SQL 連線快取沒有 Ping 驗證機制
     如果 Pod 重啟，快取的連線可能失效（Issue #1046）
```

### AgentClientSet

```
pkg/agent/client/clientset.go

┌─────────────────────────────────────────────────────────────────┐
│                      AgentClientSet                             │
├─────────────────────────────────────────────────────────────────┤
│  用於與 Pod 內的 agent sidecar HTTP API 通訊                    │
├─────────────────────────────────────────────────────────────────┤
│  Endpoints:                                                     │
│  ├── GET /gtid          → 取得 GTID（從狀態檔案）              │
│  ├── GET /health/ready  → Readiness probe                      │
│  └── GET /health/live   → Liveness probe                       │
└─────────────────────────────────────────────────────────────────┘
```

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

## 與現有筆記的關聯

| 筆記 | 主題 | 本文關聯 |
|------|------|----------|
| 07 | GTID 概念 | Role detection 使用 GTID 比較 |
| 08 | Replication 設定 | ConfigurePrimary/Replica 細節 |
| 09 | Switchover | Phase 詳解，本文補充架構視角 |
| 10 | Failover | FailoverHandler 觸發位置 |
| 12 | Error 1236 | Recovery 觸發條件之一 |
| 13 | Semi-sync | Init container 生成 cnf 設定 |
| 14 | Binlog 問題排除 | Agent 狀態檔案機制 |

## 關鍵程式碼路徑

| 功能 | 檔案 |
|------|------|
| 主 reconciler | `internal/controller/mariadb_controller.go` |
| ReplicationReconciler | `pkg/controller/replication/controller.go` |
| ConfigurePrimary/Replica | `pkg/controller/replication/config.go` |
| SQL 連線管理 | `pkg/controller/replication/clientset.go` |
| Switchover 狀態機 | `pkg/controller/replication/switchover.go` |
| Failover 候選人選擇 | `pkg/controller/replication/failover.go` |
| Pod 層級 failover 觸發 | `internal/controller/pod_replication_controller.go` |
| 狀態更新 | `internal/controller/mariadb_controller_status.go` |
| Agent server | `cmd/agent/replication.go` |
| Init container | `cmd/init/replication.go` |
| Agent HTTP handlers | `pkg/agent/handler/replication/` |
| Agent client | `pkg/agent/client/replication.go` |
