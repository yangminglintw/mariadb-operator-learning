# Replication Debug 指南

## Operator 行為控制設定（「旋鈕」）

### suspend: true/false

**定義**：`api/v1alpha1/base_types.go:997-1004`

```yaml
apiVersion: k8s.mariadb.com/v1alpha1
kind: MariaDB
spec:
  suspend: true   # 暫停所有 reconcile
```

**實作**：`internal/controller/mariadb_controller.go:964-975`

```go
func (r *MariaDBReconciler) reconcileSuspend(ctx, mariadb) (ctrl.Result, error) {
    if mariadb.IsSuspended() {
        log.FromContext(ctx).V(1).Info("MariaDB is suspended. Skipping...")
        return ctrl.Result{RequeueAfter: 10 * time.Second}, nil
    }
    return ctrl.Result{}, nil
}
```

**機制**：
- Phase 3 執行
- 返回 `RequeueAfter: 10s`
- 阻斷後續 Phase 4-21 執行

**限制**：
- 只是暫停，不會重置任何狀態
- Switchover 狀態（`ConditionTypePrimarySwitched=False`）仍然存在
- Resume 後會繼續執行卡住的 switchover

### cleanupPolicy: Skip/Delete

**定義**：`api/v1alpha1/base_types.go:1020-1028`

```yaml
apiVersion: k8s.mariadb.com/v1alpha1
kind: MariaDB
spec:
  cleanupPolicy: Delete  # 或 Skip
```

**用途**：
- 用於 Grant/User/Database CR
- `Delete`：刪除 CR 時也刪除 DB 中對應資源
- `Skip`：刪除 CR 時保留 DB 中的資源

**與 Replication 關係**：不直接相關，但影響 SQL 資源生命週期。

### syncTimeout

**定義**：`api/v1alpha1/mariadb_replication_types.go:174-182`

```yaml
spec:
  replication:
    replica:
      syncTimeout: 10s   # 預設 10s
```

**用途**：
- Switchover Phase 3：等待所有 replicas 同步到 primary GTID
- Failover：等待新 primary 處理完 relay log
- 超時後 operator 會重新開始操作

**注意**：這是 operator 端的 timeout，不是 DB 端的參數。

### connectionRetrySeconds

**定義**：`api/v1alpha1/mariadb_replication_types.go:159-163`

```yaml
spec:
  replication:
    replica:
      connectionRetrySeconds: 60   # 預設不設定
```

**用途**：
- 對應 `CHANGE MASTER TO ... MASTER_CONNECT_RETRY=60`
- MariaDB replica thread 連接失敗時的重連間隔

**注意**：這是 DB 端重試，不是 operator 端重試。

### autoFailoverDelay

**定義**：`api/v1alpha1/mariadb_replication_types.go:95-99`

```yaml
spec:
  replication:
    primary:
      autoFailover: true
      autoFailoverDelay: 30s   # 預設 0（無延遲）
```

**用途**：
- Primary Pod NotReady 後，等待多久才觸發 failover
- 避免因短暫網路問題觸發不必要的 failover

**實作**：`internal/controller/pod_replication_controller.go:103-112`

### maxLagSeconds

**定義**：`api/v1alpha1/mariadb_replication_types.go:164-173`

```yaml
spec:
  replication:
    replica:
      maxLagSeconds: 0   # 預設 0（不允許任何 lag）
```

**影響**：
- 落後的 replica 不會被選為 failover 候選人
- 落後的 replica 會阻止 switchover 和 upgrade
- 用於 secondary service endpoint 篩選

**注意**：MaxScale 使用自己的 `max_replication_lag` router 參數。

### updateStrategy

**定義**：`api/v1alpha1/mariadb_types.go:376-395`

```yaml
spec:
  updateStrategy:
    type: RollingUpdate  # 或 NeverUpdate, OnDelete
    autoUpdateDataPlane: false
```

**與 Replication 關係**：
- Rolling update 期間 Replication reconcile 仍會執行
- 可能需要 suspend 來防止衝突

### 缺失的「旋鈕」

以下功能目前**不支援**手動控制：

| 功能 | 現況 | 影響 |
|------|------|------|
| 手動取消 switchover | 無法取消 | 卡住的 switchover 需要手動修復 DB |
| 連線重試次數/策略 | 無重試 | SQL 連線失敗直接報錯 |
| Switchover 整體 timeout + 回退 | 無回退 | Phase 失敗後無限重試 |

## DB 端 Debug（MariaDB SQL）

### 確認角色

```sql
-- 檢查是否為 replica
SHOW SLAVE STATUS\G

-- 關鍵欄位：
-- Slave_IO_Running: Yes/No
-- Slave_SQL_Running: Yes/No
-- Seconds_Behind_Master: 0 表示同步
-- Last_IO_Error: 錯誤訊息（如 Error 1236）
-- Last_SQL_Error: SQL thread 錯誤
```

### GTID 狀態檢查

```sql
-- Primary 上執行（binlog 最新位置）
SELECT @@gtid_binlog_pos;

-- Replica 上執行（已複製位置）
SELECT @@gtid_slave_pos;

-- 任何節點（當前執行位置）
SELECT @@gtid_current_pos;

-- GTID 格式: domain-server_id-sequence
-- 例如: 0-10-1000
```

### Replication 健康檢查

```sql
-- IO thread 狀態
SHOW SLAVE STATUS\G
-- 看 Slave_IO_Running

-- SQL thread 狀態
-- 看 Slave_SQL_Running

-- Lag 檢查
-- 看 Seconds_Behind_Master
```

### Semi-sync 狀態

```sql
-- 檢查 semi-sync 是否啟用
SHOW GLOBAL VARIABLES LIKE 'rpl_semi_sync%';

-- 檢查 semi-sync 狀態
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync%';

-- 關鍵指標：
-- Rpl_semi_sync_master_status: ON/OFF
-- Rpl_semi_sync_slave_status: ON/OFF
-- Rpl_semi_sync_master_no_tx: 降級次數
```

### Lock 檢查

```sql
-- 檢查是否有 FLUSH TABLES WITH READ LOCK
SHOW PROCESSLIST;
-- 看是否有 "Waiting for table flush"

-- 檢查 read_only 狀態
SELECT @@read_only, @@super_read_only;
```

### Server ID 確認

```sql
SELECT @@server_id;
-- 應該 = serverIdBase + podIndex
-- 預設 serverIdBase = 10
-- Pod-0 → server_id = 10
-- Pod-1 → server_id = 11
```

## Operator 端 Debug（kubectl + logs）

### MariaDB CR status 檢查重點

```bash
kubectl get mariadb mariadb -o yaml

# 關鍵欄位：
status:
  currentPrimaryPodIndex: 0          # 目前 primary
  currentPrimary: mariadb-0          # 目前 primary pod name
  currentPrimaryFailingSince: null   # failover 計時器

  conditions:
  - type: Ready
    status: "True"
  - type: ReplicationConfigured       # 首次設定完成
    status: "True"
  - type: PrimarySwitched             # False = 正在 switchover
    status: "True"
  - type: ReplicaRecovered            # False = 正在 recovery
    status: "True"

  replication:
    roles:
      mariadb-0: Primary
      mariadb-1: Replica
      mariadb-2: Replica
    replicas:
      mariadb-1:
        slaveIORunning: true
        slaveSQLRunning: true
        secondsBehindMaster: 0
        lastIOErrno: 0
        lastSQLErrno: 0
```

### Operator 日誌關鍵字

```bash
# Switchover 相關
kubectl logs -l app.kubernetes.io/name=mariadb-operator | grep -i switchover

# Failover 相關
kubectl logs -l app.kubernetes.io/name=mariadb-operator | grep -i failover

# Replication 一般
kubectl logs -l app.kubernetes.io/name=mariadb-operator | grep -i replication

# 特定 MariaDB
kubectl logs -l app.kubernetes.io/name=mariadb-operator | grep mariadb
```

### Events 查看

```bash
kubectl get events --field-selector involvedObject.name=mariadb --sort-by='.lastTimestamp'

# 常見 Events：
# ReplicationPrimaryLock: Locking primary with read lock
# ReplicationReplicaSync: Waiting for replicas to be synced with primary
# ReplicationPrimaryNew: Configuring new primary at index 'X'
# PrimarySwitched: Primary switched from index 'X' to index 'Y'
```

### Pod 狀態歷史

```bash
kubectl describe pod mariadb-0

# 看：
# - Events 區塊
# - Last State（之前的狀態）
# - Restart Count
```

### 啟用詳細 SQL 日誌

```bash
# Operator deployment 加入 --log-sql flag
kubectl edit deployment mariadb-operator-controller-manager

# 在 args 加入：
args:
- --log-sql=true

# 這會 log 所有執行的 SQL 語句
```

## Switchover 6 Phase 完整參照

```
pkg/controller/replication/switchover.go:71-96

Phase 1: Lock primary with read lock
         SQL: SET GLOBAL read_only = ON
              FLUSH TABLES WITH READ LOCK
         失敗模式: 連線失敗、lock 取得失敗

Phase 2: Set read_only in primary
         SQL: SET GLOBAL read_only = ON
         失敗模式: 連線失敗

Phase 3: Wait sync
         檢查: 所有 replica 的 gtid_slave_pos >= primary 的 gtid_binlog_pos
         失敗模式: GTID domain 不匹配（#1499）、replica lag 太大
         ★ 這裡卡住會導致 primary 持續被鎖定

Phase 4: Configure new primary
         SQL: STOP ALL SLAVES
              RESET SLAVE ALL
              RESET MASTER（可選）
              SET GLOBAL read_only = OFF
         失敗模式: 連線失敗

Phase 5: Connect replicas to new primary
         SQL: CHANGE MASTER TO ... MASTER_USE_GTID=slave_pos
              START SLAVE
         失敗模式: 連線過期 invalid connection（#1046）

Phase 6: Change primary to replica
         SQL: UNLOCK TABLES
              CHANGE MASTER TO ...
              START SLAVE
         失敗後: 舊 primary 可能變成 limbo master（#1289）
```

### 狀態機弱點

```
┌─────────────────────────────────────────────────────────────────────┐
│  Switchover 狀態機問題                                              │
├─────────────────────────────────────────────────────────────────────┤
│  1. 只能前進，不能後退                                              │
│     - Phase 3 失敗 → 重試 Phase 3（不會解鎖 Phase 1 的 lock）      │
│                                                                     │
│  2. 無整體 timeout                                                  │
│     - 沒有「switchover 超過 5 分鐘就放棄」的機制                    │
│                                                                     │
│  3. 無自動回退                                                      │
│     - 失敗後不會自動恢復到原始狀態                                  │
│     - 需要手動修復                                                  │
└─────────────────────────────────────────────────────────────────────┘
```

## Debug 思路流程圖

```
                     Replication 異常
                           │
          ┌────────────────┴────────────────┐
          ▼                                 ▼
    DB 端檢查                        Operator 端檢查
          │                                 │
          ▼                                 ▼
    SHOW SLAVE STATUS               kubectl get mariadb -o yaml
          │                                 │
    ┌─────┴─────┐                   ┌───────┴───────┐
    ▼           ▼                   ▼               ▼
  IO Error?   SQL Error?       Switching?      Recovery?
    │           │                   │               │
    ▼           ▼                   ▼               ▼
  檢查連線    檢查資料          檢查 Phase       檢查備份
  網路問題    一致性問題        哪裡卡住         是否成功
    │           │                   │               │
    ▼           ▼                   ▼               ▼
  重啟 IO    手動修復           解鎖 primary    重試 recovery
  thread     或 recovery         或 手動修復
```

## 常見問題診斷表

| 症狀 | 可能原因 | 診斷方法 | 解決方案 |
|------|----------|----------|----------|
| Switchover 卡住 | GTID domain 不匹配 (#1499) | 檢查 `gtid_binlog_pos` domain | 手動解鎖 primary，修復 GTID |
| 兩個 Master | Limbo master (#1289) | 檢查 `status.replication.roles` | 手動將舊 primary 設為 replica |
| Invalid connection | SQL 連線快取失效 (#1046) | 檢查 Pod 重啟歷史 | 重啟 operator 或手動修復 |
| Suspend 無法恢復 | Switchover 狀態仍在 | 檢查 `PrimarySwitched` condition | 手動清除 condition 或完成 switchover |
| Replica 落後 | Relay log 未處理完 | `SHOW SLAVE STATUS` 看 lag | 等待或檢查 SQL thread |
| Connection refused | Service/Pod 問題 | `kubectl get endpoints` | 檢查 Pod Ready 狀態 |

## Webhook vs Controller：問題定位

### 時機差異

```
┌─────────────────────────────────────────────────────────────────────┐
│  Webhook（Admission Controller）                                    │
│  - 時機：CR 建立/更新請求送到 API Server 時，在寫入 etcd 之前       │
│  - 類型：Validating（檢查合法性）、Mutating（修改預設值）          │
│  - 失敗表現：kubectl apply 直接報錯，CR 不會被建立                 │
│  - 來源：internal/webhook/v1alpha1/                                │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Controller（Reconciler）                                           │
│  - 時機：CR 已經存到 etcd 之後，非同步處理                         │
│  - 失敗表現：CR 存在但 status 顯示錯誤，conditions 不是 Ready      │
│  - 來源：internal/controller/                                       │
└─────────────────────────────────────────────────────────────────────┘
```

### Debug 判斷流程

```
kubectl apply 失敗？
  ├── Yes → 問題在 Webhook
  │         └── 檢查錯誤訊息，通常是 validation 失敗
  │
  └── No  → CR 已建立，檢查 status/conditions
            └── status 異常？
                ├── Yes → 問題在 Controller
                │         └── 檢查 operator logs 和 events
                └── No  → 可能是資源還在建立中
                          └── 等待或檢查 operator 是否運作
```

### 範例

**Webhook 錯誤（#1488）**：
```bash
$ kubectl apply -f physicalbackup.yaml
Error from server: error when creating "physicalbackup.yaml":
admission webhook "vphysicalbackup.kb.io" denied the request:
spec.schedule.cron is required
```

**Controller 錯誤**：
```bash
$ kubectl get mariadb mariadb -o yaml
status:
  conditions:
  - type: Ready
    status: "False"
    reason: "ReplicationError"
    message: "error configuring replica: connection refused"
```

### 關鍵差異

| 面向 | Webhook | Controller |
|------|---------|------------|
| 執行時機 | 寫入前（同步） | 寫入後（非同步） |
| 錯誤回報 | kubectl 直接顯示 | status/events |
| 可以修改資料？ | Mutating 可以 | 只能改 status |
| 阻擋不合法資料？ | Yes | No（已存入 etcd）|

## MariaDB Log 閱讀指南

### 識別 Log 來源

**Operator Log**（JSON 格式）：
```json
{"level":"info","ts":"2024-01-15T10:30:45.123Z","logger":"mariadb","msg":"Reconciling MariaDB","namespace":"default","name":"mariadb"}
```

**MariaDB Log**：
```
2024-01-15 10:30:45 0 [Note] Starting MariaDB 11.4.2-MariaDB-ubu2404 as process 1 ...
2024-01-15 10:30:46 0 [Note] InnoDB: Buffer pool(s) load completed at 240115 10:30:46
```

### 關鍵 MariaDB Log 關鍵字

```
# IO thread 狀態
Slave I/O thread: connected to master
Slave I/O thread: received fatal error from master

# SQL thread 狀態
Slave SQL thread initialized
Slave SQL: Error executing command

# GTID 資訊
GTID position has been set to
Skipped GTID event

# 嚴重錯誤
Got fatal error 1236 from master when reading data from binary log
```

### GTID Position 格式

```
GTID: 0-1-100
      │ │ └── sequence number (單調遞增)
      │ └──── server_id
      └────── domain_id

範例：
- 0-10-1000: domain 0, server_id 10, sequence 1000
- 0-11-500:  domain 0, server_id 11, sequence 500
```

### 取得 MariaDB Log

```bash
# 直接從 Pod 取得
kubectl logs mariadb-0 -c mariadb

# 從 PVC 取得（如果 Pod 不存在）
kubectl run debug --image=busybox --rm -it --restart=Never -- \
  cat /var/lib/mysql/mariadb.err
```

## 關鍵程式碼路徑

| 功能 | 檔案 |
|------|------|
| SuspendTemplate | `api/v1alpha1/base_types.go:997-1004` |
| CleanupPolicy | `api/v1alpha1/base_types.go:1020-1028` |
| Replication spec | `api/v1alpha1/mariadb_replication_types.go` |
| reconcileSuspend() | `internal/controller/mariadb_controller.go:964-975` |
| Switchover 6 Phase | `pkg/controller/replication/switchover.go` |
| SQL ClientSet | `pkg/sql/sqlset.go` |
| ConfigureReplica | `pkg/controller/replication/config.go` |
| Pod failover 觸發 | `internal/controller/pod_replication_controller.go` |
| Webhook 實作 | `internal/webhook/v1alpha1/` |
