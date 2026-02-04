# Status in Kubernetes CRD

## 為什麼學這個？

正在進行的 Issues 需要理解 Status：

| Issue | 主題 | 與 Status 的關聯 |
|-------|------|------------------|
| #1214 | server_id configurable | `ReplicationStatus` 追蹤 primary/replica 狀態 |
| #1509 | Kill connections on switchover | Switchover 後需更新 status |

---

## What is Status?

在 Kubernetes CRD 中，每個資源都有兩個主要部分：
- **Spec** - 使用者定義的「期望狀態」(desired state)
- **Status** - 系統回報的「實際狀態」(actual state)

```
┌─────────────────────────────────────┐
│           Custom Resource           │
├─────────────────────────────────────┤
│  Spec (使用者寫入)                   │
│  - 我想要 3 個 replica              │
│  - 我想要用 10Gi storage            │
├─────────────────────────────────────┤
│  Status (Controller 更新)           │
│  - 目前有 2 個 replica ready        │
│  - 目前狀態是 "Running"             │
│  - Conditions: [Ready, Healthy...]  │
└─────────────────────────────────────┘
```

---

## Status 定義位置 (MariaDB Operator)

所有 Status struct 都定義在 `api/v1alpha1/*_types.go`：

| CRD | Status Struct | File:Line |
|-----|---------------|-----------|
| MariaDB | `MariaDBStatus` | `mariadb_types.go:641` |
| MaxScale | `MaxScaleStatus` | `maxscale_types.go:775` |
| Backup | `BackupStatus` | `backup_types.go:86` |
| Restore | `RestoreStatus` | `restore_types.go:122` |
| Database | `DatabaseStatus` | `database_types.go:35` |
| User | `UserStatus` | `user_types.go:100` |
| Grant | `GrantStatus` | `grant_types.go:52` |
| SqlJob | `SqlJobStatus` | `sqljob_types.go:81` |
| Replication | `ReplicationStatus` | `mariadb_replication_types.go:539` |

### 共用類型

| 檔案 | 用途 |
|------|------|
| `condition_types.go` | Condition 定義 |
| `base_types.go` | 共用 status 結構 (如 `CertificateStatus`) |

---

## Status 更新流程

```
Controller Reconcile Loop
         │
         ▼
┌─────────────────┐
│ 1. Get CR       │
│ 2. Do work      │
│ 3. Update Status│ ◄── r.Status().Update(ctx, &mariadb)
└─────────────────┘
```

---

## Key Concepts

1. **Status 是唯讀的** - 使用者不應直接修改 status
2. **Controller 負責更新** - 只有 controller 應該更新 status
3. **Conditions pattern** - 用 Conditions 陣列表示多種狀態
4. **Status subresource** - Kubernetes 允許獨立更新 status (不影響 spec)

---

## 相關 Kubebuilder Markers

```go
// +kubebuilder:subresource:status  // 啟用 status subresource
// +kubebuilder:printcolumn:name="Status",type="string",JSONPath=".status.phase"
```

---

## 與我的 Issues 的關聯

### #1214 server_id configurable

- `ReplicationStatus` 追蹤哪個 pod 是 primary
- server_id 計算需要 pod index
- 位置: `mariadb_replication_types.go:539`

### #1509 Kill connections on switchover

- Switchover 完成後需要更新 status
- 需要了解 status 更新時機
- 位置: `pkg/controller/replication/switchover.go`

---

## 延伸閱讀

- [ ] 閱讀 `MariaDBStatus` 完整結構
- [ ] 了解 Condition 的使用方式
- [ ] 追蹤 status 在 reconciler 中如何被更新
