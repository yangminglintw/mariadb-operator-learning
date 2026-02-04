# Binlog 問題排查指南

## mysqlbinlog 基本用法

### 基本語法

```bash
mysqlbinlog [options] log_file ...
```

### 常用選項

| 選項 | 說明 |
|------|------|
| `--base64-output=DECODE-ROWS` | 解碼 ROW 格式的 binlog |
| `-v` / `--verbose` | 顯示 ROW 格式的詳細內容 |
| `-vv` | 更詳細 (包含 column types) |
| `--start-datetime` | 從指定時間開始 |
| `--stop-datetime` | 到指定時間結束 |
| `--start-position` | 從指定 position 開始 |
| `--stop-position` | 到指定 position 結束 |
| `--database` | 只顯示指定 database 的事件 |
| `--no-defaults` | 不讀取配置文件 |

### 基本範例

```bash
# 查看 binlog 內容
mysqlbinlog mysql-bin.000001

# 解碼 ROW 格式
mysqlbinlog --base64-output=DECODE-ROWS -v mysql-bin.000001

# 查看更詳細的 column 資訊
mysqlbinlog --base64-output=DECODE-ROWS -vv mysql-bin.000001
```

---

## 常用技巧

### 1. 時間範圍過濾

```bash
# 指定開始和結束時間
mysqlbinlog --base64-output=DECODE-ROWS -v \
  --start-datetime="2024-01-15 10:00:00" \
  --stop-datetime="2024-01-15 11:00:00" \
  mysql-bin.000123

# 只看某個時間點之後
mysqlbinlog --base64-output=DECODE-ROWS -v \
  --start-datetime="2024-01-15 10:00:00" \
  mysql-bin.000123
```

**注意**: 時間格式必須是 `YYYY-MM-DD HH:MM:SS`

### 2. Position 範圍過濾

```bash
# 指定開始和結束 position
mysqlbinlog --base64-output=DECODE-ROWS -v \
  --start-position=12345 \
  --stop-position=67890 \
  mysql-bin.000123

# 從某個 position 開始到檔案結尾
mysqlbinlog --base64-output=DECODE-ROWS -v \
  --start-position=12345 \
  mysql-bin.000123
```

**如何找到 position?**

```sql
-- 在 MariaDB 中查看 binlog events
SHOW BINLOG EVENTS IN 'mysql-bin.000123' LIMIT 100;

-- 會顯示每個 event 的 Pos 和 End_log_pos
```

### 3. 只看特定 Database

```bash
# 只顯示指定 database 的事件
mysqlbinlog --base64-output=DECODE-ROWS -v \
  --database=myapp \
  mysql-bin.000123
```

**注意**: 這只對 STATEMENT 格式完全有效，ROW 格式仍可能顯示其他 database 的事件

### 4. 解析 ROW 格式

```bash
# 基本解析
mysqlbinlog --base64-output=DECODE-ROWS -v mysql-bin.000123

# 完整解析 (包含 column 類型)
mysqlbinlog --base64-output=DECODE-ROWS -vv mysql-bin.000123
```

**ROW 格式輸出範例**:
```
### INSERT INTO `mydb`.`users`
### SET
###   @1=123 /* INT meta=0 nullable=0 is_null=0 */
###   @2='john' /* VARSTRING(100) meta=100 nullable=1 is_null=0 */
###   @3='2024-01-15 10:30:00' /* DATETIME(0) meta=0 nullable=1 is_null=0 */
```

---

## 常用 Pattern

### 1. 找特定時間的變更

```bash
# 找某個時間段的所有變更
mysqlbinlog --base64-output=DECODE-ROWS -v \
  --start-datetime="2024-01-15 10:00:00" \
  --stop-datetime="2024-01-15 10:30:00" \
  mysql-bin.000123 | less

# 找變更並顯示前後文
mysqlbinlog --base64-output=DECODE-ROWS -v \
  --start-datetime="2024-01-15 10:00:00" \
  mysql-bin.000123 | grep -B 5 -A 10 "DELETE"
```

### 2. 找特定 Table 的變更

```bash
# 找某個 table 的所有變更
mysqlbinlog --base64-output=DECODE-ROWS -v mysql-bin.000123 | \
  grep -A 20 "table_name"

# 找某個 table 的 DELETE 操作
mysqlbinlog --base64-output=DECODE-ROWS -v mysql-bin.000123 | \
  grep -B 5 -A 15 "DELETE FROM \`mydb\`.\`users\`"

# 找某個 table 的 UPDATE 操作
mysqlbinlog --base64-output=DECODE-ROWS -v mysql-bin.000123 | \
  grep -B 5 -A 20 "UPDATE \`mydb\`.\`users\`"
```

### 3. 確認 ROLLBACK

```bash
# 找所有 ROLLBACK
mysqlbinlog --base64-output=DECODE-ROWS -v mysql-bin.000123 | \
  grep -B 10 "ROLLBACK"

# 找某個 GTID 的 transaction 並確認是否 ROLLBACK
mysqlbinlog --base64-output=DECODE-ROWS -v mysql-bin.000123 | \
  grep -A 50 "GTID 0-10-12345" | head -60

# 統計 COMMIT vs ROLLBACK
mysqlbinlog mysql-bin.000123 | grep -c "^COMMIT"
mysqlbinlog mysql-bin.000123 | grep -c "^ROLLBACK"
```

### 4. 統計操作數量

```bash
# 統計各種操作
mysqlbinlog --base64-output=DECODE-ROWS -v mysql-bin.000123 | \
  grep "###" | awk '{print $2}' | sort | uniq -c | sort -rn

# 輸出範例：
#   1234 INSERT
#    567 UPDATE
#     89 DELETE

# 統計每個 table 的操作數量
mysqlbinlog --base64-output=DECODE-ROWS -v mysql-bin.000123 | \
  grep "### INSERT\|### UPDATE\|### DELETE" | \
  awk '{print $2, $4}' | sort | uniq -c | sort -rn
```

### 5. 找特定 GTID 範圍

```bash
# 找特定 GTID
mysqlbinlog --base64-output=DECODE-ROWS -v mysql-bin.000123 | \
  grep -A 50 "GTID 0-10-12345"

# 找 GTID 範圍 (例如 12340 到 12350)
for i in $(seq 12340 12350); do
  echo "=== GTID 0-10-$i ==="
  mysqlbinlog --base64-output=DECODE-ROWS -v mysql-bin.000123 | \
    grep -A 30 "GTID 0-10-$i" | head -35
done
```

### 6. 產生可重新執行的 SQL

```bash
# 提取 SQL 並移除 GTID 相關設定
mysqlbinlog --base64-output=DECODE-ROWS -v \
  --start-position=12345 \
  --stop-position=67890 \
  mysql-bin.000123 | \
  grep -v "^SET @@SESSION.GTID_NEXT" | \
  grep -v "^# at" | \
  grep -v "^#[0-9]" > replay.sql
```

---

## Binlog 輸出解讀

### Event 結構

```
# at 12345                        <- binlog position
#240115 10:30:00 server id 10     <- 時間和 server_id
  end_log_pos 12456               <- 下一個 event 的 position
  CRC32 0xabcdef12                <- checksum
  Query   thread_id=123           <- event type
  exec_time=0
  error_code=0
```

### 常見 Event Types

| Event Type | 說明 |
|------------|------|
| `Query` | DDL 或 STATEMENT 格式的 DML |
| `Table_map` | 描述接下來要操作的 table |
| `Write_rows` | INSERT (ROW 格式) |
| `Update_rows` | UPDATE (ROW 格式) |
| `Delete_rows` | DELETE (ROW 格式) |
| `Xid` | Transaction commit |
| `Gtid` | GTID event |
| `Rotate` | 切換到新的 binlog 檔案 |

### ROW 格式詳細輸出

```
### INSERT INTO `mydb`.`users`
### SET
###   @1=123                    <- column 1 (通常是 id)
###   @2='john'                 <- column 2
###   @3='john@example.com'     <- column 3
```

**UPDATE 的 WHERE 和 SET**:
```
### UPDATE `mydb`.`users`
### WHERE
###   @1=123
###   @2='john'
### SET
###   @1=123
###   @2='john_updated'
```

### GTID Event 格式

```
#240115 10:30:00 server id 10  end_log_pos 12500
# GTID 0-10-12345 cid=67890 trans
```

- `0-10-12345`: domain_id-server_id-sequence
- `cid`: commit id
- `trans`: 表示這是一個 transaction

---

## 實用腳本

### 1. 找遺失的 GTID 範圍內容

```bash
#!/bin/bash
# find_gtid_range.sh

BINLOG=$1
START_SEQ=$2
END_SEQ=$3
DOMAIN_SERVER="0-10"  # 根據實際情況修改

for seq in $(seq $START_SEQ $END_SEQ); do
  gtid="${DOMAIN_SERVER}-${seq}"
  echo "========== GTID $gtid =========="
  mysqlbinlog --base64-output=DECODE-ROWS -v "$BINLOG" | \
    grep -A 50 "GTID $gtid" | head -55
  echo ""
done
```

使用方式:
```bash
./find_gtid_range.sh mysql-bin.000123 12267 12345
```

### 2. 分析 binlog 內容摘要

```bash
#!/bin/bash
# analyze_binlog.sh

BINLOG=$1

echo "=== Binlog Summary ==="
echo "File: $BINLOG"
echo ""

echo "Transaction counts:"
echo "  COMMIT: $(mysqlbinlog "$BINLOG" | grep -c '^COMMIT')"
echo "  ROLLBACK: $(mysqlbinlog "$BINLOG" | grep -c '^ROLLBACK')"
echo ""

echo "Operation counts (ROW format):"
mysqlbinlog --base64-output=DECODE-ROWS -v "$BINLOG" | \
  grep "###" | awk '{print $2}' | sort | uniq -c | sort -rn
echo ""

echo "Top tables:"
mysqlbinlog --base64-output=DECODE-ROWS -v "$BINLOG" | \
  grep "### INSERT\|### UPDATE\|### DELETE" | \
  awk -F'`' '{print $2"."$4}' | sort | uniq -c | sort -rn | head -10
```

### 3. 檢查特定時間範圍的 ROLLBACK

```bash
#!/bin/bash
# check_rollbacks.sh

BINLOG=$1
START_TIME=$2
END_TIME=$3

echo "Checking for ROLLBACKs between $START_TIME and $END_TIME"
echo ""

mysqlbinlog --start-datetime="$START_TIME" \
            --stop-datetime="$END_TIME" \
            "$BINLOG" | \
  grep -B 20 "^ROLLBACK" | \
  grep -E "GTID|ROLLBACK"
```

使用方式:
```bash
./check_rollbacks.sh mysql-bin.000123 "2024-01-15 10:00:00" "2024-01-15 11:00:00"
```

---

## 常見問題排查

### 1. 找不到 binlog 檔案

```bash
# 確認 binlog 位置
mysql -e "SHOW VARIABLES LIKE 'log_bin_basename';"

# 列出所有 binlog 檔案
mysql -e "SHOW BINARY LOGS;"

# 確認目前使用的 binlog
mysql -e "SHOW MASTER STATUS;"
```

### 2. 權限問題

```bash
# 確保有讀取權限
ls -la /var/lib/mysql/mysql-bin.*

# 如果在 container 中
kubectl exec mariadb-0 -- ls -la /var/lib/mysql/mysql-bin.*
```

### 3. Binlog 已被 purge

```bash
# 檢查最舊的 binlog
mysql -e "SHOW BINARY LOGS;" | head -5

# 如果需要的 binlog 已經被 purge，只能從 backup 恢復
```

### 4. ROW 格式看不懂 column 名稱

ROW 格式只顯示 `@1`, `@2` 等 column 編號，需要對照 table schema:

```sql
-- 查看 table schema
SHOW CREATE TABLE mydb.users;

-- 或
DESCRIBE mydb.users;
```

然後手動對應：
- @1 → 第一個 column
- @2 → 第二個 column
- ...

---

## 在 Kubernetes 中使用

### 從 Pod 中讀取 binlog

```bash
# 直接在 pod 中執行
kubectl exec mariadb-0 -- mysqlbinlog \
  --base64-output=DECODE-ROWS -v \
  /var/lib/mysql/mysql-bin.000123 | head -100

# 如果需要更複雜的分析，先 copy 出來
kubectl cp mariadb-0:/var/lib/mysql/mysql-bin.000123 ./mysql-bin.000123
mysqlbinlog --base64-output=DECODE-ROWS -v mysql-bin.000123
```

### 找 binlog 檔案位置

```bash
# 先確認 binlog 設定
kubectl exec mariadb-0 -- mysql -e \
  "SHOW VARIABLES LIKE '%log_bin%';"

# 列出 binlog 檔案
kubectl exec mariadb-0 -- mysql -e "SHOW BINARY LOGS;"

# 列出實際檔案
kubectl exec mariadb-0 -- ls -la /var/lib/mysql/mysql-bin.*
```
