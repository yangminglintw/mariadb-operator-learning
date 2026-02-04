# Error 1236 Case Study: Failover 後的 GTID 問題

## Case Study 背景

### 環境

- **Cluster 配置**: 3 nodes replication cluster (1 Primary + 2 Replicas)
- **Semi-sync**: 啟用 AFTER_SYNC 模式
- **事件序列**:
  1. Primary (Pod-0) 發生 Node NotReady
  2. Operator 自動執行 Failover
  3. Pod-1 被選為新 Primary
  4. 原 Pod-0 恢復後無法加入 Replication

### 錯誤訊息

```
Got fatal error 1236 from master when reading data from binary log:
'Cannot find GTID state requested by slave in binlog at GTID 0-10-12345'
```

---

## GTID 狀態分析

### Failover 後的 GTID 比較

```
舊 Primary (Pod-0):
  gtid_current_pos = 0-10-12345
  gtid_binlog_state = 0-10-12345

新 Primary (Pod-1):
  gtid_current_pos = 0-11-12266
  gtid_binlog_pos = 0-11-12266
  gtid_binlog_state = 0-10-12266,0-11-12266
```

### Gap 分析

```
舊 Primary 最後 GTID: 0-10-12345
新 Primary 最後同步: 0-10-12266

Gap = 12345 - 12266 = 79 transactions
```

### 為什麼會有 Gap？

1. Primary (Pod-0) 收到 79 個 transactions
2. 這些 transaction 寫入了 binlog
3. 在 replica 確認 (ACK) 前，Primary 發生故障
4. Replica 從未收到這 79 個 transactions
5. Failover 後，新 Primary 沒有這些 GTID
6. 舊 Primary 恢復後請求 GTID 0-10-12345 → Error 1236

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

## Semi-sync AFTER_SYNC 保護機制

### 為什麼有 Gap 但沒有資料遺失？

Semi-sync AFTER_SYNC 的 commit 流程：

```
Client                   Primary                  Replica
  │                         │                        │
  │── BEGIN ──────────────► │                        │
  │── INSERT... ──────────► │                        │
  │── COMMIT ─────────────► │                        │
  │                         │                        │
  │                    ┌────┴────┐                   │
  │                    │ 1. Write│                   │
  │                    │ binlog  │                   │
  │                    └────┬────┘                   │
  │                         │                        │
  │                         │── 2. Send binlog ────► │
  │                         │                        │
  │                         │◄─ 3. Wait for ACK ──── │
  │                         │                        │
  │                    ┌────┴────┐                   │
  │                    │ 4. InnoDB│                  │
  │                    │ COMMIT   │                  │
  │                    └────┬────┘                   │
  │                         │                        │
  │◄─── 5. OK ──────────────│                        │
```

### 關鍵點

1. **Write binlog** - Transaction 寫入 binlog (此時 `no_tx` 可能增加)
2. **Send to replica** - 發送給 replica
3. **Wait ACK** - 等待 replica 確認收到
4. **InnoDB COMMIT** - 只有收到 ACK 才真正 commit
5. **Return to client** - 回覆 client 成功

### 如果在 Step 3 (Wait ACK) 時 Primary crash

```
結果：
- Binlog 有記錄 (write binlog 已完成)
- InnoDB 沒有 commit (因為沒收到 ACK)
- Replica 可能沒收到 (取決於 crash 時機)
- Recovery 時 InnoDB 會 ROLLBACK 這個 transaction
```

### 所以...

```
no_tx 增加 + binlog 有 ROLLBACK = 沒有資料遺失

原因：
- Transaction 寫入 binlog 但沒有被 InnoDB commit
- Crash recovery 會 rollback 這些 transaction
- Client 不會收到成功回應
- Replica 沒有這些資料是正確的行為
```

---

## 修復 Error 1236 的方法

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

## 如果有真實 Commit 的資料要保留

### 情境

- 確認 binlog 中有些 transaction 是真正 commit 的（沒有 ROLLBACK）
- 這些資料在新 Primary 上不存在
- 需要手動恢復這些資料

### 方法 1: 使用 mysqlbinlog 識別並匯出

```bash
# 1. 識別有哪些 transactions 是真正 commit 的
mysqlbinlog --base64-output=DECODE-ROWS -v \
  /var/lib/mysql/mysql-bin.000123 | \
  grep -B 50 "COMMIT" | grep -A 50 "GTID 0-10-"

# 2. 提取這些 transaction 的 SQL
mysqlbinlog --base64-output=DECODE-ROWS -v \
  --start-position=12345 \
  --stop-position=67890 \
  /var/lib/mysql/mysql-bin.000123 > missing_data.sql

# 3. 手動檢查並在新 Primary 上執行
# 注意：需要去掉 GTID 相關的設定
```

### 方法 2: 直接匯出再匯入

```bash
# 1. 在舊 Primary 上匯出遺失的資料（假設知道是哪些表）
mysqldump -h old-primary \
  --single-transaction \
  --no-create-info \
  database_name table_name \
  --where="created_at > '2024-01-15 10:00:00'" > missing_data.sql

# 2. 在新 Primary 上匯入
mysql -h new-primary database_name < missing_data.sql
```

### 方法 3: 應用程式層面處理

如果有完整的 audit log 或 event sourcing：
1. 從 audit log 識別遺失的操作
2. 由應用程式重新執行這些操作

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
