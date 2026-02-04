# MariaDB GTID 概念

## 什麼是 GTID？

GTID (Global Transaction ID) 是 MariaDB 用來唯一識別每個 transaction 的機制，讓 replication 可以精確追蹤哪些 transaction 已經被複製。

## GTID 格式

```
<domain_id>-<server_id>-<sequence_number>
```

例如：`0-11-1234`
- `domain_id`: 0 (預設)
- `server_id`: 11 (產生此 transaction 的 server)
- `sequence_number`: 1234 (遞增序號)

### Domain ID 的用途

Domain ID 用於多 master 環境，讓不同 master 的 transaction 可以被區分：

```
Domain 0: 0-11-100, 0-11-101, 0-11-102  (Primary A)
Domain 1: 1-12-50, 1-12-51               (Primary B)
```

在 mariadb-operator 中，通常只使用 `domain_id = 0`。

## 四個重要的 GTID 變數

### 1. `gtid_binlog_pos`

**本機產生的 transactions**

```sql
SHOW GLOBAL VARIABLES LIKE 'gtid_binlog_pos';
-- 結果: 0-11-1234
```

- 只包含這台 server 「自己執行」的 write transactions
- 當執行 INSERT/UPDATE/DELETE 時遞增
- Replica 如果開啟 `log_slave_updates`，複製來的 transaction 也會記錄在這裡

### 2. `gtid_slave_pos`

**複製來的 transactions**

```sql
SHOW GLOBAL VARIABLES LIKE 'gtid_slave_pos';
-- 結果: 0-10-5678
```

- 只包含從其他 server「複製過來」的 transactions
- Replica 執行 relay log 時更新
- Primary 通常為空（因為它不從別人複製）

### 3. `gtid_current_pos`

**binlog_pos + slave_pos 的合併**

```sql
SHOW GLOBAL VARIABLES LIKE 'gtid_current_pos';
-- 結果: 0-10-5678,0-11-1234
```

- 這是判斷 server 資料狀態的主要指標
- 包含所有已 commit 的 transactions
- **用於比較哪台 replica 資料最新**

### 4. `gtid_binlog_state`

**Binlog 檔案中實際存在的 GTID 範圍**

```sql
SHOW GLOBAL VARIABLES LIKE 'gtid_binlog_state';
```

- 記錄 binlog 檔案中包含的所有 GTID
- 當 binlog 被 purge 時，這個值會改變
- **用於判斷 replica 請求的 GTID 是否還存在**

## `log_slave_updates` 的影響

這個設定決定 replica 是否把複製來的 transaction 寫入自己的 binlog。

### 開啟 (`log_slave_updates = ON`)

```
Primary (server_id=10):
  gtid_binlog_pos: 0-10-100

Replica (server_id=11, log_slave_updates=ON):
  gtid_binlog_pos: 0-10-100  ← 複製來的也記錄
  gtid_slave_pos:  0-10-100
```

**優點**：
- Replica 可以作為其他 replica 的 source
- Switchover 時其他 replica 可以從新 primary 繼續複製

**缺點**：
- 佔用更多 disk space
- Binlog 寫入增加

**mariadb-operator 預設開啟此選項**。

### 關閉 (`log_slave_updates = OFF`)

```
Replica (server_id=11, log_slave_updates=OFF):
  gtid_binlog_pos: (空或只有本地 transaction)
  gtid_slave_pos:  0-10-100
```

## `RESET MASTER` 的影響

`RESET MASTER` 會清除所有 binlog 並重置 GTID 狀態。

### 執行前
```sql
gtid_binlog_pos:   0-10-100
gtid_slave_pos:    0-10-100
gtid_current_pos:  0-10-100
gtid_binlog_state: 0-10-100
```

### 執行後
```sql
RESET MASTER;

gtid_binlog_pos:   (空)
gtid_slave_pos:    0-10-100  ← 保持不變！
gtid_current_pos:  0-10-100  ← 來自 slave_pos
gtid_binlog_state: (空)
```

### 為什麼要用 `RESET MASTER`？

在 mariadb-operator 的 `ConfigurePrimary()` 中：

```go
// 新 primary 之前是 replica，有 slave_pos
// 執行 RESET MASTER 清除舊的 binlog state
// 但保留 slave_pos，讓其他 replica 知道從哪裡繼續
```

這確保：
1. 新 primary 的 binlog 從乾淨狀態開始
2. `gtid_current_pos` 仍然正確（來自 slave_pos）
3. 其他 replica 可以用這個 position 繼續複製

## GTID 比較

在 failover 時，operator 需要比較哪台 replica 資料最新：

```go
// pkg/controller/replication/replication.go
func compareGTID(a, b string) int {
    // 解析 GTID: "0-10-100" → domain=0, server=10, seq=100
    // 比較 sequence number
}
```

比較邏輯：
1. 解析兩個 GTID 字串
2. 找到相同 domain 的 GTID
3. 比較 sequence number
4. Sequence 較大的表示資料較新

## 相關程式碼位置

- GTID 解析: `pkg/controller/replication/replication.go`
- Primary 設定: `pkg/controller/galera/replication.go:ConfigurePrimary()`
- Replica 設定: `pkg/controller/galera/replication.go:ConfigureReplica()`
- GTID 比較: `pkg/controller/replication/failover.go:FurthestAdvancedReplica()`

## 常見問題

### Q: 為什麼 replica 的 gtid_current_pos 比 primary 大？

可能原因：
1. Primary 發生 failover，新 primary 之前是 replica
2. `RESET MASTER` 後 binlog_pos 被清除
3. 但 slave_pos 保留了之前複製的 GTID

### Q: Error 1236 是什麼？

```
Got fatal error 1236 from master:
'Cannot find GTID state requested by slave'
```

表示 replica 請求的 GTID 在 primary 的 binlog 中已經不存在（被 purge 了）。

### Q: 如何查看目前的 replication 狀態？

```sql
-- 在 replica 上執行
SHOW SLAVE STATUS\G

-- 重要欄位
Gtid_IO_Pos:     目前正在接收的 GTID
Gtid_Slave_Pos:  已經執行完的 GTID
Seconds_Behind_Master: 落後秒數
```
