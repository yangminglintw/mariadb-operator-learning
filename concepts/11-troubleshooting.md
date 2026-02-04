# MariaDB Operator 問題排查

## Error 1236 分析

### 錯誤訊息

```
Got fatal error 1236 from master when reading data from binary log:
'Cannot find GTID state requested by slave in binlog at GTID [gtid]'
```

### 原因

Replica 請求的 GTID 在 Primary 的 binlog 中已經不存在。

```
情境：
  Replica 請求: 從 GTID 0-10-100 開始
  Primary binlog: 只有 0-10-500 ~ 0-10-1000
  結果: Error 1236

原因：
  1. Primary 的 binlog 被 purge 了
  2. Replica 太久沒有連線
  3. Failover 後 GTID 不連續
```

### 解決方式

**方法 1: 重新同步 replica**

```bash
# 1. 在 replica 上停止 replication
mysql> STOP SLAVE;

# 2. 從 primary 取得完整 backup
mysqldump -h primary --all-databases --master-data=2 > backup.sql

# 3. 在 replica 上還原
mysql < backup.sql

# 4. 設定新的 GTID position
mysql> SET GLOBAL gtid_slave_pos = 'new-gtid';

# 5. 重新啟動 replication
mysql> START SLAVE;
```

**方法 2: 使用 mariabackup**

```bash
# 1. 在 primary 上備份
mariabackup --backup --target-dir=/backup

# 2. 準備備份
mariabackup --prepare --target-dir=/backup

# 3. 在 replica 上還原
mariabackup --copy-back --target-dir=/backup

# 4. 設定 GTID 並啟動 replication
```

### 預防措施

```sql
-- 增加 binlog 保留時間
SET GLOBAL binlog_expire_logs_seconds = 604800;  -- 7 天

-- 或增加 binlog 檔案數量
SET GLOBAL max_binlog_files = 100;
```

## Corner Cases

### 1. 所有 GTID 相同

**情境**：
```
Pod-0: gtid_current_pos = 0-10-100
Pod-1: gtid_current_pos = 0-10-100
Pod-2: gtid_current_pos = 0-10-100
```

**處理**：選擇 pod index 最小的作為新 primary。

**為什麼會發生**：
- Primary 沒有新的 write
- 所有 replica 都已經 sync

### 2. 沒有合格候選人

**情境**：
```
Pod-0 (Primary): NotReady
Pod-1: NotReady
Pod-2: Slave_IO_Running = No
```

**處理**：不執行 failover，等待人工介入。

**排查步驟**：
```bash
# 1. 檢查所有 pod 狀態
kubectl get pods -l app.kubernetes.io/instance=mariadb

# 2. 檢查 pod 詳細狀態
kubectl describe pod mariadb-1

# 3. 檢查 MariaDB logs
kubectl logs mariadb-1

# 4. 手動檢查 replication 狀態
kubectl exec mariadb-2 -- mysql -e "SHOW SLAVE STATUS\G"
```

### 3. Relay Log Pending

**情境**：
```
Replica:
  IO Thread: 已接收到 GTID 0-10-200
  SQL Thread: 已執行到 GTID 0-10-150
  Relay log: 50 個 pending transactions
```

**問題**：
- Failover 時使用 `gtid_current_pos` (0-10-150)
- 50 個 transaction 可能遺失

**檢查方式**：
```sql
SHOW SLAVE STATUS\G

-- 檢查這些欄位：
Gtid_IO_Pos:        0-10-200  -- 已接收
Exec_Master_Log_Pos: 12345    -- 已執行的 binlog position
```

**處理**：
- 等待 SQL thread 追上
- 或接受可能的資料遺失

### 4. Split Brain

**情境**：
```
Network partition:
  Zone A: Pod-0 (認為自己是 primary)
  Zone B: Pod-1, Pod-2 (選出 Pod-1 為新 primary)

結果: 兩個 primary 同時接受 writes
```

**預防措施**：

1. **增加 failover delay**
   ```yaml
   spec:
     replication:
       replica:
         automaticFailoverDelay: 60s
   ```

2. **使用 semi-sync replication**
   - Primary 必須等待 replica 確認才 commit
   - 如果連不上任何 replica，write 會 timeout

3. **STONITH (Shoot The Other Node In The Head)**
   - 確保舊 primary 無法寫入
   - 例如：fencing 機制

**偵測方式**：
```sql
-- 檢查是否有多個 read_only = OFF 的節點
SELECT @@read_only;
```

**修復方式**：
```bash
# 1. 確認哪個 primary 有較新的資料
# 2. 將另一個設為 read_only
# 3. 使用較新的作為真正的 primary
# 4. 修復 replication
```

## Readiness Probe 檢查內容

### 預設檢查

```yaml
readinessProbe:
  exec:
    command:
      - bash
      - -c
      - mysql -u root -p${MARIADB_ROOT_PASSWORD} -e "SELECT 1"
  initialDelaySeconds: 10
  periodSeconds: 5
  failureThreshold: 3
```

### 檢查內容

1. MariaDB process 運作中
2. 可以接受連線
3. 可以執行 query

### 增強版檢查（可選）

```yaml
readinessProbe:
  exec:
    command:
      - bash
      - -c
      - |
        mysql -u root -p${MARIADB_ROOT_PASSWORD} -e "
          SELECT 1;
          SHOW SLAVE STATUS;
        " && \
        # 檢查 replication lag
        lag=$(mysql -u root -p${MARIADB_ROOT_PASSWORD} -N -e \
          "SHOW SLAVE STATUS\G" | grep Seconds_Behind_Master | awk '{print $2}')
        if [ "$lag" != "NULL" ] && [ "$lag" -gt 300 ]; then
          exit 1
        fi
```

## Pod NotReady 的情況

### 常見原因

| 原因 | 症狀 | 解決方式 |
|------|------|----------|
| OOM Killed | Container restart, exit code 137 | 增加 memory limit |
| Disk full | Error log 顯示 disk full | 清理 disk 或增加 PVC |
| Too many connections | "Too many connections" error | 增加 max_connections |
| Slow query | Readiness timeout | 優化 query 或增加 timeout |
| Corrupted data | InnoDB crash recovery 失敗 | 從 backup 還原 |

### 診斷步驟

```bash
# 1. 檢查 pod 狀態
kubectl get pod mariadb-0 -o yaml

# 2. 檢查 events
kubectl describe pod mariadb-0

# 3. 檢查容器 logs
kubectl logs mariadb-0 -c mariadb
kubectl logs mariadb-0 -c mariadb --previous  # 如果重啟了

# 4. 檢查 node 狀態
kubectl get node -o wide
kubectl describe node <node-name>

# 5. 進入容器檢查
kubectl exec -it mariadb-0 -- bash
```

### 常見 Log 訊息

```
# OOM
"Out of memory; check if mysqld or some other process uses all available memory"

# Disk full
"Disk is full writing './mysql-bin.000123'"

# Corrupted page
"InnoDB: Database page corruption on disk or a failed file read"

# Replication error
"Slave SQL thread: Error 'Duplicate entry' on query"
```

## 實用診斷 SQL

### 檢查 Replication 狀態

```sql
-- 完整 slave 狀態
SHOW SLAVE STATUS\G

-- 重要欄位：
-- Slave_IO_Running: Yes
-- Slave_SQL_Running: Yes
-- Seconds_Behind_Master: 0
-- Last_Error: (空)
```

### 檢查 GTID 狀態

```sql
-- 所有 GTID 變數
SHOW GLOBAL VARIABLES LIKE 'gtid%';

-- 目前位置
SELECT @@gtid_current_pos;
```

### 檢查連線

```sql
-- 所有連線
SHOW PROCESSLIST;

-- 詳細連線資訊
SHOW FULL PROCESSLIST;

-- 連線統計
SHOW STATUS LIKE 'Threads%';
```

### 檢查 Binlog

```sql
-- Binlog 列表
SHOW BINARY LOGS;

-- Binlog 事件
SHOW BINLOG EVENTS IN 'mysql-bin.000001' LIMIT 10;
```

## Error 1236 Runbook (Quick Reference)

### 快速診斷流程

```bash
# Step 1: 確認 Error 1236 發生
kubectl exec mariadb-0 -- mysql -e "SHOW SLAVE STATUS\G" | grep -E "Last_Error|Slave_"

# Step 2: 比較 GTID
for i in 0 1 2; do
  echo "=== mariadb-$i ==="
  kubectl exec mariadb-$i -- mysql -e \
    "SELECT @@gtid_current_pos, @@gtid_binlog_pos, @@gtid_slave_pos;"
done

# Step 3: 確認目前的 Primary
kubectl get mariadb mariadb -o jsonpath='{.status.currentPrimaryPodIndex}'
```

### 檢查 Semi-sync no_tx

```bash
# 在有問題的 Pod 上檢查
kubectl exec mariadb-0 -- mysql -e \
  "SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_master_%';"

# 重點看：
# - Rpl_semi_sync_master_no_tx: 累計未確認數
# - Rpl_semi_sync_master_status: ON/OFF
```

### 檢查 Binlog 有無 ROLLBACK

```bash
# 進入 Pod
kubectl exec -it mariadb-0 -- bash

# 找最近的 GTID 並檢查
mysqlbinlog --base64-output=DECODE-ROWS -v \
  /var/lib/mysql/mysql-bin.000XXX | \
  grep -B 10 "ROLLBACK" | tail -20
```

### 決策表

| no_tx 增加? | 有 ROLLBACK? | 動作 |
|-------------|--------------|------|
| 否 | - | 檢查其他原因 (binlog purge?) |
| 是 | 全部是 | `RESET MASTER` 修復 |
| 是 | 部分不是 | 匯出資料後重建 |
| 是 | 否 | 可能有真實資料遺失，謹慎處理 |

### 修復方法 1: RESET MASTER

```sql
-- 在有問題的 Pod 上執行 (確認沒有資料遺失時)
STOP SLAVE;
RESET MASTER;
CHANGE MASTER TO
  MASTER_HOST = 'mariadb-1.mariadb-internal',  -- 新 Primary
  MASTER_USER = 'replication',
  MASTER_PASSWORD = 'xxx',
  MASTER_USE_GTID = slave_pos;
START SLAVE;
SHOW SLAVE STATUS\G
```

### 修復方法 2: 完整重建

```bash
# 刪除 PVC 和 Pod，讓 StatefulSet 重建
kubectl delete pvc data-mariadb-0
kubectl delete pod mariadb-0
# 等待重建完成
kubectl get pods -w
```

---

## Node NotReady Failover 處理

### 情境

Primary Pod 所在的 Node 發生 NotReady，Operator 執行自動 Failover。

### 處理流程

```
1. Node NotReady
       │
       ▼
2. Pod 變成 Unknown/NotReady
       │
       ▼
3. 等待 automaticFailoverDelay (預設 60s)
       │
       ▼
4. Operator 執行 Failover
       │
       ├──► 選出新 Primary (最新 GTID)
       │
       ▼
5. Node 恢復
       │
       ├──► 舊 Primary 變成 Replica
       │
       ▼
6. 可能發生 Error 1236
```

### 預防措施

```yaml
spec:
  replication:
    replica:
      # 增加延遲，避免短暫問題觸發 failover
      automaticFailoverDelay: 120s

    # 啟用 semi-sync
    primary:
      semiSync:
        enabled: true
        timeout: 10s
```

### 快速診斷

```bash
# 1. 檢查 Node 狀態
kubectl get nodes

# 2. 檢查 Pod 狀態
kubectl get pods -l app.kubernetes.io/instance=mariadb -o wide

# 3. 檢查 Operator logs (找 failover 相關)
kubectl logs deployment/mariadb-operator -n mariadb-operator | \
  grep -i "failover\|primary" | tail -50

# 4. 檢查 MariaDB CR 狀態
kubectl get mariadb mariadb -o jsonpath='{.status}' | jq
```

### 恢復後的檢查清單

```bash
# 1. 確認所有 Pod 都 Ready
kubectl get pods -l app.kubernetes.io/instance=mariadb

# 2. 確認 Replication 正常
for i in 1 2; do
  echo "=== mariadb-$i ==="
  kubectl exec mariadb-$i -- mysql -e "SHOW SLAVE STATUS\G" | \
    grep -E "Slave_IO|Slave_SQL|Last_Error|Seconds_Behind"
done

# 3. 確認 GTID 正在同步
kubectl exec mariadb-1 -- mysql -e "SELECT @@gtid_current_pos;"
sleep 5
kubectl exec mariadb-1 -- mysql -e "SELECT @@gtid_current_pos;"
```

---

## Operator 排查

### 檢查 Operator Logs

```bash
# Operator deployment logs
kubectl logs -f deployment/mariadb-operator -n mariadb-operator

# 過濾特定關鍵字
kubectl logs deployment/mariadb-operator -n mariadb-operator | grep -i error
kubectl logs deployment/mariadb-operator -n mariadb-operator | grep -i failover
kubectl logs deployment/mariadb-operator -n mariadb-operator | grep -i switchover
```

### 檢查 MariaDB CR 狀態

```bash
# 完整狀態
kubectl get mariadb mariadb -o yaml

# 特定欄位
kubectl get mariadb mariadb -o jsonpath='{.status}'

# Conditions
kubectl get mariadb mariadb -o jsonpath='{.status.conditions}' | jq
```

### 常見 Condition

| Condition | Status | 意義 |
|-----------|--------|------|
| Ready | True | MariaDB 正常運作 |
| Ready | False | 有問題 |
| ReplicationConfigured | True | Replication 設定完成 |
| PrimaryReady | True | Primary 正常 |
| PrimaryReady | False | Primary 有問題 |

## 緊急處理 Runbook

### Primary 掛了

```bash
# 1. 確認狀態
kubectl get pods -l app.kubernetes.io/instance=mariadb
kubectl get mariadb mariadb -o jsonpath='{.status.currentPrimaryPodIndex}'

# 2. 如果 failover 沒有自動發生，檢查原因
kubectl logs deployment/mariadb-operator -n mariadb-operator | tail -100

# 3. 手動觸發 failover（修改 podIndex）
kubectl patch mariadb mariadb --type=merge -p '{"spec":{"replication":{"primary":{"podIndex":1}}}}'
```

### Replication 斷了

```bash
# 1. 檢查 slave 狀態
kubectl exec mariadb-1 -- mysql -e "SHOW SLAVE STATUS\G"

# 2. 如果是 Error 1236，參考上面的解決方式

# 3. 嘗試重新啟動 replication
kubectl exec mariadb-1 -- mysql -e "STOP SLAVE; START SLAVE;"
```

### 需要完整重建

```bash
# 1. 刪除 pod 讓它重新建立
kubectl delete pod mariadb-1

# 2. 如果 PVC 資料損壞，可能需要刪除 PVC
kubectl delete pvc data-mariadb-1

# 3. 重新建立 pod
# StatefulSet 會自動重建
```
