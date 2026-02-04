# Replication Issues 分析

**日期**: 2026-01-22
**分析目的**: 對齊 #1543 Multi-cluster Topology Roadmap

---

## 待做 Issues 清單

### 優先順序

| 優先 | Issue | 標題 | 複雜度 | 狀態 |
|------|-------|------|--------|------|
| 1 | #1214 | server_id configurable | 中等 | 準備留言 |
| 2 | #1508 | Replica recovery (sql_log_bin) | 低 | 待分析確認 |
| 3 | #1509 | Switchover kill connections | 中等 | Relevant to my use case |

---

## #1214: Make server_id configurable

### 問題
- `server_id` 硬編碼為 `10 + podIndex`
- Multi-cluster replication 需要不同 cluster 有不同的 server_id 範圍

### 關鍵程式碼
`pkg/controller/replication/config.go:311-317`:
```go
func serverId(podName string) (int, error) {
    podIndex, err := statefulset.PodIndex(podName)
    if err != nil {
        return 0, fmt.Errorf("error getting Pod index: %v", err)
    }
    return 10 + *podIndex, nil  // ← "10" 需要可設定
}
```

### 解決方案
1. 在 `ReplicationSpec` 加入 `ServerIdBase *int`
2. 透過環境變數傳遞到 init container
3. 修改 `serverId()` 使用可設定的 base

### 留言範本
```
Hi @mmontes11,

I'd like to contribute to this issue as part of the multi-cluster topology roadmap (#1543).

My proposed approach:
1. Add `serverIdBase` field to `ReplicationSpec` in `api/v1alpha1/mariadb_replication_types.go`
2. Pass it via environment variable to the init container
3. Modify `serverId()` in `pkg/controller/replication/config.go` to use the configurable base

This would allow different clusters to have non-overlapping server_id ranges:
- Cluster A: serverIdBase=100 → server_ids: 100, 101, 102
- Cluster B: serverIdBase=200 → server_ids: 200, 201, 202

Would this approach align with your plans? Any specific requirements I should consider?
```

---

## #1508: Replica Recovery 失敗

### 問題
Replica recovery 時出現錯誤：
```
Error 'Operation ALTER USER failed for 'repl'@'%'' on query
```

### 根因
`pkg/sql/sql.go` 的 `AlterUser` 函數沒有使用 `SET sql_log_bin=0`，導致 DDL 被寫入 binlog。

### 解決方案
```go
func (c *Client) AlterUser(ctx context.Context, accountName string, ...) error {
    // 停用 binlog 避免 replication 問題
    if err := c.Exec(ctx, "SET sql_log_bin=0"); err != nil {
        return fmt.Errorf("error disabling sql_log_bin: %v", err)
    }
    defer c.Exec(ctx, "SET sql_log_bin=1")

    // ... existing code ...
}
```

需要修改的函數：`CreateUser`, `AlterUser`, `DropUser`, `Grant`, `Revoke`

---

## #1509: Switchover 後 Client 卡住

### 問題
Graceful switchover 後，client 仍連到舊 primary (現在是 read-only)，導致 write 永久失敗。

### 根因
K8s conntrack 保持舊 TCP 連線，即使 Service endpoint 已更新。

### 關鍵程式碼
`pkg/controller/replication/switchover.go:71-96`:
```go
phases := []switchoverPhase{
    {name: "Lock primary with read lock", ...},
    {name: "Set read_only in primary", ...},
    {name: "Wait sync", ...},
    {name: "Configure new primary", ...},
    {name: "Connect replicas to new primary", ...},
    {name: "Change primary to replica", ...},
    // ❌ 沒有斷開舊連線的步驟！
}
```

### 解決方案
在 `changePrimaryToReplica` 後加入新 phase：
```go
{
    name:      "Kill user connections on old primary",
    reconcile: r.killUserConnectionsOnOldPrimary,
},
```

### 留言範本
```
Hi @mmontes11,

I've analyzed this issue and found that the root cause is Kubernetes conntrack keeping
existing TCP connections alive even after the service endpoint changes.

I'd like to propose a solution: Add a new phase in switchover to kill user connections
on the old primary after changing it to replica. This would involve:

1. Adding a `KillUserConnections` method to the SQL client
2. Adding a new switchover phase after `changePrimaryToReplica`

Would you be open to this approach? I'm happy to implement it if approved.

Also, should this behavior be:
- Default (always kill connections on switchover)
- Configurable via a new field like `spec.replication.primary.killConnectionsOnSwitchover`

Looking forward to your feedback!
```

---

## #1543 Multi-cluster Roadmap 相關 Issues

```
#1543 Multi-cluster Topology (Roadmap Priority)
├── #1214 server_id configurable ← Working on this
├── #1530 gtid_domain_id per pod
├── #1504 gtid_domain_id (duplicate)
├── #1218 Primary from external
├── #878 galera.cnf editing
├── PR #1437 External replication (進行中)
└── ...more issues
```

---

## 學到的概念

### Replication 相關
- **server_id**: 每個 MariaDB instance 需要唯一的 ID
- **GTID**: Global Transaction ID，用於追蹤 replication position
- **Semi-sync**: 已在 operator 中實作
- **Binlog**: Binary log，記錄所有變更

### Switchover 流程
1. Lock primary with read lock
2. Set read_only in primary
3. Wait for replicas to sync
4. Configure new primary
5. Connect replicas to new primary
6. Change old primary to replica

### sql_log_bin
- `SET sql_log_bin=0` 可以讓 SQL 不被寫入 binlog
- 用於避免 DDL 被複製到 replica

---

## 下一步

1. [ ] 在 #1214 留言
2. [ ] 等待 maintainer 回覆
3. [ ] 開始實作 #1214
4. [ ] 之後處理 #1508 和 #1509
