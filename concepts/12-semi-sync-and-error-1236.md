# Semi-Sync Replication 與 Error 1236

## Semi-sync 基本概念

### Async vs Semi-sync 比較

#### Asynchronous Replication

```
Client              Primary                 Replica
  │                    │                       │
  │── COMMIT ────────► │                       │
  │                    │                       │
  │               ┌────┴────┐                  │
  │               │ Write   │                  │
  │               │ binlog  │                  │
  │               └────┬────┘                  │
  │               ┌────┴────┐                  │
  │               │ InnoDB  │                  │
  │               │ COMMIT  │                  │
  │               └────┬────┘                  │
  │                    │                       │
  │◄─── OK ────────────│                       │
  │                    │                       │
  │                    │── Send binlog ──────► │ (背景)
  │                    │                       │
```

**特點**:
- Client 收到 OK 時，replica 可能還沒收到資料
- Primary crash 時可能遺失已確認的 transactions
- 效能最好，延遲最低

#### Semi-sync AFTER_COMMIT (舊版預設)

```
Client              Primary                 Replica
  │                    │                       │
  │── COMMIT ────────► │                       │
  │                    │                       │
  │               ┌────┴────┐                  │
  │               │ Write   │                  │
  │               │ binlog  │                  │
  │               └────┬────┘                  │
  │               ┌────┴────┐                  │
  │               │ InnoDB  │                  │
  │               │ COMMIT  │◄─────────────────┤ 問題：已經 commit 了！
  │               └────┬────┘                  │
  │                    │                       │
  │                    │── Send binlog ──────► │
  │                    │                       │
  │                    │◄─── ACK ──────────────│
  │                    │                       │
  │◄─── OK ────────────│                       │
```

**問題**:
- Commit 在 send binlog 之前
- 如果 commit 後、send 前 crash → phantom read
- 其他 session 可能已經看到這筆資料

#### Semi-sync AFTER_SYNC (推薦，MariaDB 10.3+ 預設)

```
Client              Primary                 Replica
  │                    │                       │
  │── COMMIT ────────► │                       │
  │                    │                       │
  │               ┌────┴────┐                  │
  │               │ Write   │                  │
  │               │ binlog  │                  │
  │               └────┬────┘                  │
  │                    │                       │
  │                    │── Send binlog ──────► │
  │                    │                       │
  │                    │◄─── ACK ──────────────│
  │                    │                       │
  │               ┌────┴────┐                  │
  │               │ InnoDB  │◄─────────────────┤ 重點：收到 ACK 才 commit！
  │               │ COMMIT  │                  │
  │               └────┬────┘                  │
  │                    │                       │
  │◄─── OK ────────────│                       │
```

**優點**:
- 沒有 phantom read 問題
- 如果 ACK 前 crash，InnoDB 會 rollback
- Replica 一定有已確認的資料

---

## WaitPoint 機制

### AFTER_SYNC vs AFTER_COMMIT

| 面向 | AFTER_SYNC | AFTER_COMMIT |
|------|------------|--------------|
| ACK 時機 | InnoDB commit 前 | InnoDB commit 後 |
| Phantom read | 不會發生 | 可能發生 |
| Crash 後資料一致性 | 保證 | 不保證 |
| 效能 | 略差 | 略好 |
| MariaDB 預設 | 10.3+ 預設 | 舊版預設 |

### 設定方式

```sql
-- 查看目前設定
SHOW GLOBAL VARIABLES LIKE 'rpl_semi_sync_master_wait_point';

-- 設定為 AFTER_SYNC（推薦）
SET GLOBAL rpl_semi_sync_master_wait_point = 'AFTER_SYNC';
```

---

## Error 1236 問題分析

### 錯誤訊息

```
Got fatal error 1236 from master when reading data from binary log:
'Cannot find GTID state requested by slave in binlog at GTID 0-10-12345'
```

### 原因

Replica 請求的 GTID 在 Primary 的 binlog 中已經不存在。

```
情境：
  Replica 請求: 從 GTID 0-10-100 開始
  Primary binlog: 只有 0-10-500 ~ 0-10-1000
  結果: Error 1236

常見原因：
  1. Primary 的 binlog 被 purge 了
  2. Replica 太久沒有連線
  3. Failover 後 GTID 不連續
```

### Case Study：Failover 後的 GTID Gap

#### 環境
- **Cluster 配置**: 3 nodes replication cluster (1 Primary + 2 Replicas)
- **Semi-sync**: 啟用 AFTER_SYNC 模式
- **事件序列**:
  1. Primary (Pod-0) 發生 Node NotReady
  2. Operator 自動執行 Failover
  3. Pod-1 被選為新 Primary
  4. 原 Pod-0 恢復後無法加入 Replication

#### GTID 比較

```
舊 Primary (Pod-0):
  gtid_current_pos = 0-10-12345
  gtid_binlog_state = 0-10-12345

新 Primary (Pod-1):
  gtid_current_pos = 0-11-12266
  gtid_binlog_pos = 0-11-12266
  gtid_binlog_state = 0-10-12266,0-11-12266
```

#### Gap 分析

```
舊 Primary 最後 GTID: 0-10-12345
新 Primary 最後同步: 0-10-12266

Gap = 12345 - 12266 = 79 transactions
```

#### 為什麼會有 Gap？

1. Primary (Pod-0) 收到 79 個 transactions
2. 這些 transaction 寫入了 binlog
3. 在 replica 確認 (ACK) 前，Primary 發生故障
4. Replica 從未收到這 79 個 transactions
5. Failover 後，新 Primary 沒有這些 GTID
6. 舊 Primary 恢復後請求 GTID 0-10-12345 → Error 1236

---

## Semi-sync 如何防止 Error 1236

### AFTER_SYNC 的保護機制

當 Primary crash 在 Step 3 (Wait ACK) 時：

```
結果：
- Binlog 有記錄 (write binlog 已完成)
- InnoDB 沒有 commit (因為沒收到 ACK)
- Replica 可能沒收到 (取決於 crash 時機)
- Recovery 時 InnoDB 會 ROLLBACK 這個 transaction
```

### 重點

```
no_tx 增加 + binlog 有 ROLLBACK = 沒有資料遺失

原因：
- Transaction 寫入 binlog 但沒有被 InnoDB commit
- Crash recovery 會 rollback 這些 transaction
- Client 不會收到成功回應
- Replica 沒有這些資料是正確的行為
```

---

## 如何判斷是否有資料遺失

### 檢查流程

```
┌─────────────────────────────────────────────────────────┐
│  Step 1: 檢查 Semi-sync no_tx 是否增加                   │
│  SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_no_tx';  │
└─────────────────────────────────────────────────────────┘
                         │
            ┌────────────┴────────────┐
            │                         │
    ┌───────▼────────┐      ┌────────▼────────┐
    │ no_tx 沒有增加  │      │ no_tx 有增加     │
    │ (相比 failover │      │ (failover 期間)  │
    │  前的值)       │      │                  │
    └───────┬────────┘      └────────┬────────┘
            │                        │
            ▼                        ▼
    ┌───────────────┐      ┌─────────────────────┐
    │ 正常情況      │      │ Step 2: 檢查 binlog │
    │ 沒有未確認的  │      │ 有無 ROLLBACK       │
    │ transactions │      └─────────┬───────────┘
    └───────────────┘                │
                          ┌─────────┴─────────┐
                          │                   │
                  ┌───────▼───────┐   ┌───────▼───────┐
                  │ 有 ROLLBACK   │   │ 沒有 ROLLBACK │
                  │               │   │               │
                  └───────┬───────┘   └───────┬───────┘
                          │                   │
                          ▼                   ▼
                  ┌───────────────┐   ┌───────────────────┐
                  │ 沒有資料遺失  │   │ 可能有真實 commit │
                  │ 可以安全處理  │   │ 需要進一步確認    │
                  └───────────────┘   └───────────────────┘
```

### Step 1: 檢查 no_tx

```sql
-- 在舊 Primary 上執行
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_no_tx';

-- 重點：看「增量」，不是絕對值
-- 如果 failover 前是 0，現在是 79 → 有 79 個未確認的 tx
-- 如果 failover 前是 50，現在是 50 → 沒有新的未確認 tx
```

**注意**: `no_tx` 是累計計數器，需要比較 failover 前後的差值。

### Step 2: 檢查 Binlog 有無 ROLLBACK

```bash
# 找到 Gap 中的 transactions
mysqlbinlog --base64-output=DECODE-ROWS -v \
  /var/lib/mysql/mysql-bin.000123 | \
  grep -A 5 "GTID 0-10-12267"
```

尋找的 Pattern:
```
# GTID 0-10-12345
ROLLBACK
```

---

## 監控指標與告警

### Semi-sync Metrics 一覽

```sql
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync%';
```

| 變數名稱 | 類型 | 說明 |
|---------|------|------|
| `Rpl_semi_sync_master_status` | 狀態 | ON = semi-sync 啟用中, OFF = 降級為 async |
| `Rpl_semi_sync_master_yes_tx` | 計數器 | 成功收到 ACK 的 transactions 累計數 |
| `Rpl_semi_sync_master_no_tx` | 計數器 | 未收到 ACK (timeout) 的 transactions 累計數 |
| `Rpl_semi_sync_master_clients` | 數量 | 目前連接的 semi-sync replica 數量 |

### Prometheus Alert Rules

#### 1. Semi-sync 降級 Alert

```yaml
groups:
- name: mariadb-semi-sync
  rules:
  - alert: MariaDBSemiSyncDisabled
    expr: mysql_global_status_rpl_semi_sync_master_status == 0
    for: 1m
    labels:
      severity: warning
    annotations:
      summary: "MariaDB semi-sync disabled on {{ $labels.instance }}"
      description: "Semi-sync has been disabled for more than 1 minute."
```

#### 2. no_tx 增加 Alert

```yaml
  - alert: MariaDBSemiSyncNoTxIncreasing
    expr: increase(mysql_global_status_rpl_semi_sync_master_no_tx[5m]) > 0
    for: 0m
    labels:
      severity: warning
    annotations:
      summary: "MariaDB semi-sync no_tx increasing on {{ $labels.instance }}"
      description: "{{ $value }} transactions committed without ACK in 5 minutes."
```

#### 3. 沒有 Replica 連線

```yaml
  - alert: MariaDBSemiSyncNoClients
    expr: mysql_global_status_rpl_semi_sync_master_clients == 0
    for: 30s
    labels:
      severity: critical
    annotations:
      summary: "No semi-sync replicas connected to {{ $labels.instance }}"
```

---

## 修復 Error 1236 的方法

### 決策表

| no_tx 增加? | 有 ROLLBACK? | 動作 |
|-------------|--------------|------|
| 否 | - | 檢查其他原因 (binlog purge?) |
| 是 | 全部是 | `RESET MASTER` 修復 |
| 是 | 部分不是 | 匯出資料後重建 |
| 是 | 否 | 可能有真實資料遺失，謹慎處理 |

### 方法 1: RESET MASTER (確認沒有資料遺失時)

**適用條件**:
- 確認 `no_tx` 增加的都是 ROLLBACK
- 沒有真實被 commit 的資料

**步驟**:

```sql
-- 在舊 Primary (Pod-0) 上執行

-- 1. 確認目前是 replica 狀態
SHOW SLAVE STATUS\G

-- 2. 停止 replication
STOP SLAVE;

-- 3. 清除本地 binlog 並重設 GTID state
--    這會讓 gtid_binlog_pos 與 gtid_slave_pos 一致
RESET MASTER;

-- 4. 設定指向新 Primary
CHANGE MASTER TO
  MASTER_HOST = 'mariadb-1.mariadb-internal',
  MASTER_USER = 'replication',
  MASTER_PASSWORD = 'xxx',
  MASTER_USE_GTID = slave_pos;

-- 5. 啟動 replication
START SLAVE;

-- 6. 確認狀態
SHOW SLAVE STATUS\G
```

**為什麼這樣做有效？**

```
RESET MASTER 前：
  gtid_binlog_state = 0-10-12345  (本地產生的，包含 rollback 的)
  gtid_slave_pos = 0-10-12266     (從 replica 複製來的)

RESET MASTER 後：
  gtid_binlog_state = (空)
  gtid_slave_pos = 0-10-12266     (不變)
  gtid_current_pos = 0-10-12266   (= slave_pos)

結果：
  向新 Primary 請求 GTID 0-10-12266 之後的資料
  新 Primary 有這些資料 → 成功
```

### 方法 2: 完整重建 (有疑慮時)

**適用條件**:
- 不確定是否有資料遺失
- 有真實 commit 的資料需要特殊處理
- 資料一致性要求極高

**步驟**:

```bash
# 1. 在 Kubernetes 中刪除舊 Primary 的 PVC
kubectl delete pvc data-mariadb-0

# 2. 刪除 Pod 讓 StatefulSet 重建
kubectl delete pod mariadb-0

# 3. 新 Pod 會自動初始化並設定為 replica
# 4. Operator 會自動從新 Primary 同步資料
```

---

## 實戰設定建議

### 確保 Semi-sync 不降級

#### 1. 設定合理的 Timeout

```sql
-- 預設是 10 秒
SET GLOBAL rpl_semi_sync_master_timeout = 30000;  -- 30 秒
```

**權衡**: Timeout 太大會導致 replica 掛掉時寫入卡住

#### 2. 設定 wait_for_slave_count

```sql
-- 至少等待 N 個 replica 確認
SET GLOBAL rpl_semi_sync_master_wait_for_slave_count = 1;  -- 預設
SET GLOBAL rpl_semi_sync_master_wait_for_slave_count = 2;  -- 更安全
```

#### 3. wait_no_slave 設定

```sql
-- 沒有 replica 時的行為
SET GLOBAL rpl_semi_sync_master_wait_no_slave = ON;   -- 等待直到有 replica
SET GLOBAL rpl_semi_sync_master_wait_no_slave = OFF;  -- 直接降級為 async
```

### 配置建議

**3 節點 cluster**:
```sql
rpl_semi_sync_master_timeout = 10000        -- 10 秒
rpl_semi_sync_master_wait_for_slave_count = 1
rpl_semi_sync_master_wait_no_slave = ON
```

**5 節點 cluster (高可用)**:
```sql
rpl_semi_sync_master_timeout = 10000        -- 10 秒
rpl_semi_sync_master_wait_for_slave_count = 2
rpl_semi_sync_master_wait_no_slave = ON
```

### Binlog 設定配合

```sql
-- 確保 binlog 格式支援
SET GLOBAL binlog_format = 'ROW';

-- 確保 sync_binlog 設定
SET GLOBAL sync_binlog = 1;  -- 每次 commit 都 fsync

-- 確保 InnoDB 設定
SET GLOBAL innodb_flush_log_at_trx_commit = 1;
```

---

## 總結

### 決策流程圖

```
Error 1236 發生
        │
        ▼
檢查 no_tx 增量
        │
        ├──► 沒增加 ──► 其他原因造成，需要進一步排查
        │
        ▼
     有增加
        │
        ▼
檢查 binlog 有無 ROLLBACK
        │
        ├──► 全部都是 ROLLBACK ──► 使用 RESET MASTER 修復
        │
        ▼
   有真實 COMMIT
        │
        ├──► 資料重要 ──► 匯出後重建
        │
        ▼
   資料不重要 ──► 完整重建 (刪除 PVC)
```

### 重點記憶

1. **no_tx 看增量** - 不是絕對值，要比較 failover 前後
2. **ROLLBACK = 安全** - Semi-sync AFTER_SYNC 的保護機制
3. **RESET MASTER 很強大** - 但要確認沒有真實資料遺失
4. **有疑慮就重建** - 資料一致性比效率重要
