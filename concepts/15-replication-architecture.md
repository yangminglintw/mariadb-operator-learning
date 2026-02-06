# Replication 控制器架構

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

---

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

---

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

---

## 與現有筆記的關聯

| 筆記 | 主題 | 本文關聯 |
|------|------|----------|
| 07 | GTID 概念 | Role detection 使用 GTID 比較 |
| 08 | Replication 設定 | ConfigurePrimary/Replica 細節 |
| 09 | Switchover | Phase 詳解，本文補充架構視角 |
| 10 | Failover | FailoverHandler 觸發位置 |
| 12 | Semi-sync & Error 1236 | Recovery 觸發條件之一 |
| 13 | Binlog 問題排除 | Agent 狀態檔案機制 |

---

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
