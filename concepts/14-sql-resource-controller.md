# SQL 資源 Controller 架構

## 概述

mariadb-operator 使用共用的 SQL Controller 框架來管理需要與 MariaDB 互動的資源：
- **Database** - 資料庫
- **User** - 使用者
- **Grant** - 權限

這三種資源都遵循相同的架構模式，實現了程式碼重用和一致的行為。

---

## Kubernetes Finalizer 機制

### 什麼是 Finalizer？

Finalizer 是 Kubernetes 的一種機制，用於在刪除資源前執行清理邏輯。

```yaml
metadata:
  finalizers:
    - grant.k8s.mariadb.com/finalizer
```

### Finalizer 生命週期

```
┌─────────────────────────────────────────────────────────────────┐
│                     資源建立流程                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. 使用者建立 CR (e.g. Grant)                                   │
│           │                                                     │
│           ▼                                                     │
│  2. Controller reconcile                                        │
│           │                                                     │
│           ▼                                                     │
│  3. 在 MariaDB 執行 SQL (e.g. GRANT)                            │
│           │                                                     │
│           ▼                                                     │
│  4. AddFinalizer() ← 保護資源不被直接刪除                         │
│           │                                                     │
│           ▼                                                     │
│  5. 更新 Status → Ready                                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                     資源刪除流程                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. 使用者刪除 CR (kubectl delete)                               │
│           │                                                     │
│           ▼                                                     │
│  2. Kubernetes 設定 DeletionTimestamp                           │
│     (資源進入 Terminating 狀態)                                  │
│           │                                                     │
│           ▼                                                     │
│  3. Controller 偵測到 IsBeingDeleted() == true                  │
│           │                                                     │
│           ▼                                                     │
│  4. Finalize() - 在 MariaDB 執行清理 SQL                        │
│     (e.g. REVOKE, DROP USER, DROP DATABASE)                     │
│           │                                                     │
│           ▼                                                     │
│  5. RemoveFinalizer()                                           │
│           │                                                     │
│           ▼                                                     │
│  6. Kubernetes 真正刪除資源                                      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 為什麼需要 Finalizer？

如果沒有 Finalizer：
- `kubectl delete grant my-grant` 會立即刪除 K8s 資源
- 但 MariaDB 中的 GRANT 權限仍然存在
- 造成 K8s 狀態與 MariaDB 狀態不一致

---

## SQL 資源的三層架構

### 架構圖

```
┌─────────────────────────────────────────────────────────────────┐
│                    Layer 1: Specific Controller                 │
│         (GrantReconciler / UserReconciler / DatabaseReconciler) │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  職責：                                                          │
│  - 接收 reconcile 請求                                           │
│  - 建立 WrappedReconciler 和 WrappedFinalizer                   │
│  - 委派給 SqlReconciler                                          │
│                                                                 │
│  程式碼路徑：                                                     │
│  - internal/controller/grant_controller.go                      │
│  - internal/controller/user_controller.go                       │
│  - internal/controller/database_controller.go                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Layer 2: Generic SQL Controller              │
│                   (SqlReconciler / SqlFinalizer)                │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  職責：                                                          │
│  - 等待 MariaDB 就緒                                             │
│  - 建立 SQL 連線                                                 │
│  - 處理錯誤和重試                                                │
│  - 管理 Finalizer 生命週期                                       │
│  - 更新 Status                                                   │
│                                                                 │
│  程式碼路徑：                                                     │
│  - pkg/controller/sql/controller.go                             │
│  - pkg/controller/sql/finalizer.go                              │
│  - pkg/controller/sql/types.go                                  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│               Layer 3: Wrapped Reconciler/Finalizer             │
│          (wrappedGrantReconciler / wrappedGrantFinalizer)       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  職責：                                                          │
│  - 實作具體的 SQL 操作                                           │
│  - Grant: GRANT / REVOKE 權限                                   │
│  - User: CREATE / ALTER / DROP USER                             │
│  - Database: CREATE / DROP DATABASE                             │
│                                                                 │
│  程式碼路徑：                                                     │
│  - internal/controller/grant_controller.go (wrappedGrantReconciler)        │
│  - internal/controller/grant_controller_finalizer.go (wrappedGrantFinalizer)│
│  - internal/controller/user_controller.go (wrappedUserReconciler)          │
│  - internal/controller/user_controller_finalizer.go (wrappedUserFinalizer) │
│  - internal/controller/database_controller.go (wrappedDatabaseReconciler)  │
│  - internal/controller/database_controller_finalizer.go                     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Interface 定義

```go
// pkg/controller/sql/types.go

// Resource - SQL 資源必須實作的介面
type Resource interface {
    metav1.Object
    MariaDBRef() *mariadbv1alpha1.MariaDBRef
    IsBeingDeleted() bool
    RequeueInterval() *metav1.Duration
    RetryInterval() *metav1.Duration
    CleanupPolicy() *mariadbv1alpha1.CleanupPolicy
}

// WrappedReconciler - 具體資源的 Reconcile 邏輯
type WrappedReconciler interface {
    Reconcile(context.Context, *sqlClient.Client) error
    PatchStatus(context.Context, condition.Patcher) error
}

// WrappedFinalizer - 具體資源的 Finalize 邏輯
type WrappedFinalizer interface {
    AddFinalizer(context.Context) error
    RemoveFinalizer(context.Context) error
    ContainsFinalizer() bool
    Reconcile(context.Context, *sqlClient.Client) error  // 清理 SQL 資源
}
```

---

## Grant 完整流程

### Reconcile 流程

```
┌─────────────────────────────────────────────────────────────────┐
│                    Grant Reconcile 流程                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  GrantReconciler.Reconcile()                                    │
│  [internal/controller/grant_controller.go:41]                   │
│           │                                                     │
│           ▼                                                     │
│  SqlReconciler.Reconcile()                                      │
│  [pkg/controller/sql/controller.go:78]                          │
│           │                                                     │
│           ├─── IsBeingDeleted()? ──Yes──► Finalize 流程         │
│           │                                                     │
│           ▼ No                                                  │
│  RefResolver.MariaDBObject() - 取得 MariaDB 參照                │
│  [pkg/controller/sql/controller.go:86]                          │
│           │                                                     │
│           ▼                                                     │
│  waitForMariaDB() - 等待 MariaDB 就緒                           │
│  [pkg/controller/sql/controller.go:97]                          │
│           │                                                     │
│           ▼                                                     │
│  sqlClient.NewClientWithMariaDB() - 建立 SQL 連線               │
│  [pkg/controller/sql/controller.go:111]                         │
│           │                                                     │
│           ▼                                                     │
│  wrappedGrantReconciler.Reconcile() - 執行 GRANT SQL            │
│  [internal/controller/grant_controller.go:86]                   │
│           │                                                     │
│           ├─── 有舊權限需要撤銷？ ──Yes──► mdbClient.Revoke()    │
│           │                                                     │
│           ▼                                                     │
│  mdbClient.Grant() - 授予新權限                                  │
│  [internal/controller/grant_controller.go:107]                  │
│           │                                                     │
│           ▼                                                     │
│  AddFinalizer() - 新增 Finalizer                                │
│  [pkg/controller/sql/controller.go:136]                         │
│           │                                                     │
│           ▼                                                     │
│  PatchStatus() - 更新狀態為 Ready                               │
│  [pkg/controller/sql/controller.go:140]                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Finalize 流程

```
┌─────────────────────────────────────────────────────────────────┐
│                    Grant Finalize 流程                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  SqlReconciler.Reconcile() 偵測到 IsBeingDeleted()              │
│  [pkg/controller/sql/controller.go:79]                          │
│           │                                                     │
│           ▼                                                     │
│  SqlFinalizer.Finalize()                                        │
│  [pkg/controller/sql/finalizer.go:53]                           │
│           │                                                     │
│           ├─── ContainsFinalizer()? ──No──► return (直接結束)   │
│           │                                                     │
│           ▼ Yes                                                 │
│  RefResolver.MariaDB() - 取得 MariaDB                           │
│  [pkg/controller/sql/finalizer.go:58]                           │
│           │                                                     │
│           ├─── MariaDB NotFound? ──Yes──► RemoveFinalizer()     │
│           │    (MariaDB 已刪除，無需清理 SQL)                    │
│           │                                                     │
│           ├─── MariaDB 正在刪除? ──Yes──► RemoveFinalizer()     │
│           │    [pkg/controller/sql/finalizer.go:70]             │
│           │                                                     │
│           ▼ No                                                  │
│  waitForMariaDB() - 等待 MariaDB 就緒                           │
│  [pkg/controller/sql/finalizer.go:77]                           │
│           │                                                     │
│           ▼                                                     │
│  sqlClient.NewClientWithMariaDB() - 建立 SQL 連線               │
│  [pkg/controller/sql/finalizer.go:82]                           │
│           │                                                     │
│           ├─── 連線失敗? ──Yes──► 回傳錯誤 (會重試)              │
│           │                                                     │
│           ▼ 連線成功                                            │
│  檢查 CleanupPolicy                                             │
│  [pkg/controller/sql/finalizer.go:88]                           │
│           │                                                     │
│           ├─── CleanupPolicy == Skip? ──Yes──► 跳過 SQL 清理    │
│           │                                                     │
│           ▼ CleanupPolicy == Delete (預設)                      │
│  wrappedGrantFinalizer.Reconcile() - 執行清理 SQL               │
│  [pkg/controller/sql/finalizer.go:92]                           │
│           │                                                     │
│           ▼                                                     │
│  mdbClient.UserExists() - 檢查使用者是否存在                     │
│  [internal/controller/grant_controller_finalizer.go:53]         │
│           │                                                     │
│           ├─── User 不存在? ──Yes──► 跳過 REVOKE (無需撤銷)     │
│           │                                                     │
│           ▼ User 存在                                           │
│  mdbClient.Revoke() - 撤銷權限                                  │
│  [internal/controller/grant_controller_finalizer.go:66]         │
│           │                                                     │
│           ▼                                                     │
│  RemoveFinalizer() - 移除 Finalizer                             │
│  [pkg/controller/sql/finalizer.go:97]                           │
│           │                                                     │
│           ▼                                                     │
│  Kubernetes 完成刪除資源                                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 程式碼路徑快速參照

| 元件 | 檔案路徑 | 行號 |
|------|----------|------|
| GrantReconciler | internal/controller/grant_controller.go | 17-69 |
| wrappedGrantReconciler | internal/controller/grant_controller.go | 71-155 |
| wrappedGrantFinalizer | internal/controller/grant_controller_finalizer.go | 18-88 |
| SqlReconciler | pkg/controller/sql/controller.go | 47-187 |
| SqlFinalizer | pkg/controller/sql/finalizer.go | 18-101 |
| Interface 定義 | pkg/controller/sql/types.go | 13-41 |

---

## CleanupPolicy 選項

```go
type CleanupPolicy string

const (
    CleanupPolicyDelete CleanupPolicy = "Delete"  // 預設：刪除 SQL 資源
    CleanupPolicySkip   CleanupPolicy = "Skip"    // 跳過清理，保留 SQL 資源
)
```

使用 `Skip` 的場景：
- 不希望刪除 K8s CR 時影響 MariaDB 中的資料
- 資料庫由外部系統管理

---

## 相關學習筆記

- [01-kubernetes-operator.md](./01-kubernetes-operator.md) - Operator 基礎概念
- [02-reconcile-loop.md](./02-reconcile-loop.md) - Reconcile Loop 詳解
