# InnoDB Row Lock 診斷指南

## 背景

Row lock 是 InnoDB 正常行為 — 每個 `UPDATE`、`DELETE`、`SELECT ... FOR UPDATE` 都會取得 row lock。
問題出在「某個 transaction 持有 lock 太久」，讓其他 transaction 等到超時（`innodb_lock_wait_timeout` 預設 50 秒），
應用端收到 `ERROR 1205 (HY000): Lock wait timeout exceeded; try restarting transaction`。

本指南解決一個核心問題：**誰鎖了誰？鎖住的 SQL 是什麼？**

**與其他指南的關係**：
- `08-how-to-read-slow-query-output.md` 的 `Lock_time` 欄位記錄的就是等待 row lock 的時間。當 `Lock_time > 0.1s`，可用本指南即時診斷。
- `01-no-index-monitoring-concepts.md` 的 Performance Schema 概念同樣適用於 lock 監控。

---

## 一、Row Lock 基本概念

### Lock 類型

| Lock 類型 | 說明 | 觸發情境 |
|-----------|------|---------|
| Shared Lock (S) | 讀鎖，多個 transaction 可同時持有 | `SELECT ... LOCK IN SHARE MODE` |
| Exclusive Lock (X) | 寫鎖，同一行只有一個 transaction 能持有 | `UPDATE`, `DELETE`, `INSERT`, `SELECT ... FOR UPDATE` |
| Gap Lock | 鎖定 index 範圍間的間隙，防止 phantom read | REPEATABLE READ 下的 range query |
| Next-Key Lock | Record Lock + Gap Lock 的組合 | InnoDB 在 REPEATABLE READ 下的預設行為 |
| Intention Lock (IS/IX) | Table-level，表示 transaction 打算在表中取 S/X lock | 自動取得，通常不會造成衝突 |

### Lock Wait Chain

```
Transaction A (trx_id: 100)            Transaction B (trx_id: 200)
  BEGIN;                                 BEGIN;
  UPDATE orders SET status='done'        UPDATE orders SET status='cancel'
  WHERE id = 5;                          WHERE id = 5;
       │                                      │
       │  持有 X lock on row id=5              │  請求 X lock on row id=5
       │                                      │
       └───── B 必須等 A COMMIT/ROLLBACK ──────┘
                      Lock Wait!
```

最常見的情境：Transaction A 做完 UPDATE 但**還沒 COMMIT**（應用端在做其他事），
Transaction B 嘗試更新同一行就會卡住。

---

## 二、MariaDB Lock 監控表

MariaDB 使用 `information_schema` 下的三張表。查詢成本極低（讀記憶體，不做 I/O）。

> **MariaDB vs MySQL 8 差異**：MySQL 8.0 移除了 `information_schema.INNODB_LOCKS` 和 `INNODB_LOCK_WAITS`，
> 改用 `performance_schema.data_locks` 和 `data_lock_waits`。MariaDB 10.x/11.x 仍然使用 `information_schema` 版本。

| 表 | 說明 | 何時有資料 |
|----|------|-----------|
| `INNODB_TRX` | 所有活躍 InnoDB transaction | 有 active transaction 時 |
| `INNODB_LOCKS` | 正在被等待 + 等待中的 lock | **只在有 lock wait 時才出現** |
| `INNODB_LOCK_WAITS` | lock wait 對應關係（誰等誰） | **只在有 lock wait 時才出現** |

### INNODB_TRX 重要欄位

| 欄位 | 說明 |
|------|------|
| `trx_id` | Transaction ID |
| `trx_state` | `RUNNING` 或 `LOCK WAIT` |
| `trx_started` | Transaction 開始時間 |
| `trx_wait_started` | 開始等待 lock 的時間（NULL = 沒在等） |
| `trx_mysql_thread_id` | 對應 `SHOW PROCESSLIST` 的 Id（可用 `KILL` 終止） |
| `trx_query` | 當前正在執行的 SQL（**可能是 NULL — 見第五節**） |
| `trx_rows_locked` | 此 transaction 鎖住的行數 |
| `trx_rows_modified` | 此 transaction 修改的行數 |

### INNODB_LOCKS 重要欄位

| 欄位 | 說明 |
|------|------|
| `lock_trx_id` | 持有此 lock 的 transaction ID |
| `lock_mode` | `S`, `X`, `IS`, `IX`, `S,GAP`, `X,GAP` 等 |
| `lock_type` | `RECORD`（row lock）或 `TABLE` |
| `lock_table` | 被鎖的表（含 database 名，如 `` `mydb`.`orders` ``） |
| `lock_index` | 被鎖的 index 名稱 |
| `lock_data` | 被鎖的行 primary key 值 |

---

## 三、診斷查詢

### Query 1：顯示所有 Lock Wait（誰鎖了誰）— 最重要的查詢

```sql
SELECT
  r.trx_id              AS waiting_trx_id,
  r.trx_mysql_thread_id AS waiting_thread,
  r.trx_query           AS waiting_query,
  b.trx_id              AS blocking_trx_id,
  b.trx_mysql_thread_id AS blocking_thread,
  b.trx_query           AS blocking_query,
  b.trx_started         AS blocking_started,
  TIMESTAMPDIFF(SECOND, b.trx_started, NOW()) AS blocking_age_sec,
  l.lock_table          AS locked_table,
  l.lock_index          AS locked_index,
  l.lock_mode           AS lock_mode,
  l.lock_data           AS lock_data
FROM information_schema.INNODB_LOCK_WAITS w
JOIN information_schema.INNODB_TRX b ON b.trx_id = w.blocking_trx_id
JOIN information_schema.INNODB_TRX r ON r.trx_id = w.requesting_trx_id
JOIN information_schema.INNODB_LOCKS l ON l.lock_id = w.requested_lock_id\G
```

**解讀重點**：
- `waiting_query` — 正在等 lock 的 SQL（就是用戶看到卡住的那條）
- `blocking_query` — 持有 lock 的 SQL（**常為 NULL，見第五節**）
- `blocking_age_sec` — blocking transaction 已經跑了多久（越大越可疑）
- `lock_data` — 被鎖的行 PK 值，可以知道是哪一行

### Query 2：所有活躍 Transaction（找潛在 Lock Holder）

```sql
SELECT
  trx_id,
  trx_state,
  trx_started,
  TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_sec,
  trx_mysql_thread_id AS thread_id,
  trx_query,
  trx_rows_locked,
  trx_rows_modified
FROM information_schema.INNODB_TRX
ORDER BY trx_started ASC\G
```

`trx_started` 最早的 transaction 最可疑 — 長時間未 commit 的 transaction 是 lock 問題的主因。

### Query 3：查 Blocking Thread 的完整資訊

當 `INNODB_TRX.trx_query` 為 NULL 時，從 PROCESSLIST 看 thread 狀態：

```sql
SELECT Id, User, Host, db, Command, Time, State, Info
FROM information_schema.PROCESSLIST
WHERE Id = <blocking_thread_id>\G
```

通常會看到 `Command = Sleep` — 表示 AP 開了 transaction 做完 SQL 但沒 commit，連線在空閒。

### Query 4：SHOW ENGINE INNODB STATUS

```sql
SHOW ENGINE INNODB STATUS\G
```

重點看兩個區段：
- **TRANSACTIONS** — 每個 active transaction 的 lock 細節
- **LATEST DETECTED DEADLOCK** — 最近一次 deadlock 的完整資訊（見第七節）

### Query 5：SHOW OPEN TABLES（Table-Level Lock 輔助）

```sql
SHOW OPEN TABLES WHERE In_use > 0;
```

如果回傳結果，表示有 table-level lock（如 `LOCK TABLES` 或 `FLUSH TABLES WITH READ LOCK`）。
Row lock 問題不會出現在這裡，但排查時可以一併檢查。

---

## 四、診斷腳本

```bash
#!/bin/bash
# check_row_locks.sh
# 用法：./check_row_locks.sh <context> <namespace> <statefulset>
# 一鍵診斷所有 pod 的 InnoDB row lock 狀態

CONTEXT="$1"
NAMESPACE="$2"
STS_NAME="$3"

if [ -z "$STS_NAME" ]; then
  echo "Usage: $0 <context> <namespace> <statefulset>"
  exit 1
fi

# Pod 名稱從 StatefulSet replicas 數生成（避免 label 誤匹配 MaxScale 等非 DB pod）
REPLICAS=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get sts "${STS_NAME}" \
  -o jsonpath='{.spec.replicas}')
if [ -z "$REPLICAS" ] || [ "$REPLICAS" -eq 0 ]; then
  echo "Error: StatefulSet ${STS_NAME} not found or has 0 replicas"
  exit 1
fi
PODS=""
for i in $(seq 0 $((REPLICAS - 1))); do
  PODS="${PODS} ${STS_NAME}-${i}"
done

# 從容器環境變數取得 root 密碼
MYSQL_PWD=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
  exec "${STS_NAME}-0" -c mariadb -- printenv MARIADB_ROOT_PASSWORD)

# 從 CR status 取得 Primary pod 名稱
CURRENT_PRIMARY=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
  get mariadb "${STS_NAME}" -o jsonpath='{.status.currentPrimary}')

get_role() {
  if [ "$1" = "$CURRENT_PRIMARY" ]; then
    echo "Primary"
  else
    echo "Replica"
  fi
}

for POD in $PODS; do
  ROLE=$(get_role "$POD")
  echo "=== ${POD} (${ROLE}) ==="

  # Step 1: 檢查是否有 lock wait
  LOCK_WAITS=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
    exec "${POD}" -c mariadb -- \
    mariadb -u root -p"${MYSQL_PWD}" -N -e \
    "SELECT COUNT(*) FROM information_schema.INNODB_LOCK_WAITS" 2>/dev/null)

  if [ "$LOCK_WAITS" -gt 0 ] 2>/dev/null; then
    echo "  [WARN] Lock waits found: ${LOCK_WAITS}"
    echo ""

    # Step 2: 顯示 lock wait 詳情（誰鎖了誰）
    kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
      exec "${POD}" -c mariadb -- \
      mariadb -u root -p"${MYSQL_PWD}" -e "
        SELECT
          r.trx_id              AS waiting_trx_id,
          r.trx_mysql_thread_id AS waiting_thread,
          r.trx_query           AS waiting_query,
          b.trx_id              AS blocking_trx_id,
          b.trx_mysql_thread_id AS blocking_thread,
          b.trx_query           AS blocking_query,
          TIMESTAMPDIFF(SECOND, b.trx_started, NOW()) AS blocking_age_sec,
          l.lock_table          AS locked_table,
          l.lock_index          AS locked_index,
          l.lock_mode           AS lock_mode,
          l.lock_data           AS lock_data
        FROM information_schema.INNODB_LOCK_WAITS w
        JOIN information_schema.INNODB_TRX b ON b.trx_id = w.blocking_trx_id
        JOIN information_schema.INNODB_TRX r ON r.trx_id = w.requesting_trx_id
        JOIN information_schema.INNODB_LOCKS l ON l.lock_id = w.requested_lock_id\G" 2>/dev/null
  else
    echo "  [OK] No lock waits"
  fi

  # Step 3: 顯示長時間 transaction（> 30 秒）
  LONG_TRX=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
    exec "${POD}" -c mariadb -- \
    mariadb -u root -p"${MYSQL_PWD}" -N -e "
      SELECT
        trx_id,
        trx_state,
        TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_sec,
        trx_mysql_thread_id AS thread_id,
        trx_rows_locked,
        LEFT(trx_query, 100) AS query_truncated
      FROM information_schema.INNODB_TRX
      WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 30
      ORDER BY trx_started ASC" 2>/dev/null)

  if [ -n "$LONG_TRX" ]; then
    echo ""
    echo "  [WARN] Long-running transactions (> 30s):"
    echo "  trx_id | state | age_sec | thread_id | rows_locked | query"
    echo "  $LONG_TRX" | while IFS=$'\t' read -r TRX_ID STATE AGE THREAD LOCKED QUERY; do
      printf "  %-8s  %-10s  %6ss  thread=%-6s  locked=%-4s  %s\n" \
        "$TRX_ID" "$STATE" "$AGE" "$THREAD" "$LOCKED" "$QUERY"
    done
  fi

  echo ""
done
```

### 使用範例

```bash
# 執行
bash check_row_locks.sh kind-mdb default mariadb-chaos

# 預期輸出（正常時）
=== mariadb-chaos-0 (Primary) ===
  [OK] No lock waits

=== mariadb-chaos-1 (Replica) ===
  [OK] No lock waits

=== mariadb-chaos-2 (Replica) ===
  [OK] No lock waits
```

```
# 預期輸出（有 lock wait 時）
=== mariadb-chaos-0 (Primary) ===
  [WARN] Lock waits found: 1

  *************************** 1. row ***************************
    waiting_trx_id: 200
    waiting_thread: 15
     waiting_query: UPDATE orders SET status='cancel' WHERE id=5
   blocking_trx_id: 100
   blocking_thread: 12
    blocking_query: NULL
  blocking_age_sec: 45
      locked_table: `mydb`.`orders`
      locked_index: PRIMARY
         lock_mode: X
         lock_data: 5

  [WARN] Long-running transactions (> 30s):
  trx_id | state | age_sec | thread_id | rows_locked | query
  100       RUNNING        45s  thread=12      locked=1     NULL
```

### 效能安全策略

| 操作 | 影響 | 說明 |
|------|------|------|
| `SELECT ... FROM information_schema.INNODB_*` | 極低 — 讀記憶體 | 三張表都是記憶體結構，無 I/O |
| `SHOW ENGINE INNODB STATUS` | 極低 | 快照當前狀態 |
| `SHOW OPEN TABLES` | 低 | 掃描 table cache |
| `SELECT ... FROM performance_schema.events_statements_history` | 低 | 讀記憶體 ring buffer |

---

## 五、blocking_query 為 NULL 的處理

這是**最常見**的情境。blocking_query 為 NULL 不是 bug，而是因為：

```
Transaction A:
  BEGIN;
  UPDATE orders SET status = 'done' WHERE id = 5;  -- 這步已經跑完
  -- （AP 在做其他事，還沒 COMMIT）                  -- 目前 idle，trx_query = NULL

Transaction B:
  UPDATE orders SET status = 'cancel' WHERE id = 5; -- LOCK WAIT!
```

`trx_query` 只記錄**當前正在執行的 SQL**。Transaction A 的 UPDATE 已經跑完了（只是沒 commit），
所以 `trx_query` 是 NULL。

### 追溯方法 1：Performance Schema 歷史

查該 thread 最近執行過的 SQL：

```sql
SELECT event_id, sql_text, CURRENT_SCHEMA, ROWS_AFFECTED
FROM performance_schema.events_statements_history
WHERE thread_id = (
  SELECT THREAD_ID FROM performance_schema.threads
  WHERE PROCESSLIST_ID = <blocking_thread_id>
)
ORDER BY event_id DESC
LIMIT 5\G
```

> **前提**：`performance_schema` 必須啟用（MariaDB Operator 預設啟用）。
> `events_statements_history` 是 ring buffer，只保留最近 10 條（可透過 `performance_schema_events_statements_history_size` 調整）。

### 追溯方法 2：General Log

如果 Performance Schema 歷史已被覆蓋，可暫時開啟 general log：

```sql
SET GLOBAL general_log = ON;
-- 重現問題後
SET GLOBAL general_log = OFF;
```

> **注意**：General log 記錄所有 SQL，I/O 成本極高。只在診斷時短暫開啟。

---

## 六、終止 Blocking Transaction

確認 blocking thread id 後，可以選擇終止：

| 方法 | 指令 | 效果 | 適用時機 |
|------|------|------|---------|
| KILL 連線 | `KILL <thread_id>` | 中斷連線，transaction 自動 rollback | blocking query 為 NULL（idle transaction） |
| KILL 查詢 | `KILL QUERY <thread_id>` | 只中止當前 SQL，transaction 不 rollback | blocking query 正在跑（如大型 UPDATE） |
| 等待 timeout | 不操作 | waiting transaction 超時後放棄（ERROR 1205） | 影響可控，不想強制介入 |

> **注意**：KILL 只影響 waiting 方的 transaction rollback 嗎？不是 — `KILL <thread_id>` 是殺 **blocking** 方的連線，
> blocking transaction 會 rollback，waiting transaction 隨即取得 lock 繼續執行。

### kubectl 執行範例

```bash
# 取得密碼
MYSQL_PWD=$(kubectl --context=kind-mdb -n default \
  exec mariadb-chaos-0 -c mariadb -- printenv MARIADB_ROOT_PASSWORD)

# 終止 blocking thread（假設 thread_id = 12）
kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- \
  mariadb -u root -p"${MYSQL_PWD}" -e "KILL 12"
```

---

## 七、Deadlock 分析

Deadlock 和 lock wait 不同 — InnoDB 會**自動偵測**並立即 rollback 其中一方。

| 項目 | Lock Wait | Deadlock |
|------|-----------|----------|
| 偵測 | `INNODB_LOCK_WAITS` 有記錄 | `SHOW ENGINE INNODB STATUS` DEADLOCK section |
| InnoDB 行為 | 等待直到 timeout | 立即偵測，自動 rollback cost 較低的 transaction |
| 應用端收到 | `ERROR 1205 Lock wait timeout` | `ERROR 1213 Deadlock found` |
| 需要人工處理 | 可能需要 KILL blocker | 不需要，InnoDB 已自動處理 |
| 重點 | 找出誰沒 commit | 優化 SQL 執行順序避免再發生 |

### 查看最近 Deadlock

```sql
SHOW ENGINE INNODB STATUS\G
```

找 `LATEST DETECTED DEADLOCK` 區段，會顯示：
- TRANSACTION 1 WAITING — 第一個 transaction 等待的 lock
- TRANSACTION 2 WAITING — 第二個 transaction 等待的 lock
- WE ROLL BACK TRANSACTION N — InnoDB 選擇 rollback 哪一個

### 記錄所有 Deadlock

預設只保留最近一次 deadlock 資訊。如果要記錄所有 deadlock 到 error log：

```sql
-- 查看當前設定
SELECT @@innodb_print_all_deadlocks;

-- 開啟（動態設定，不需重啟）
SET GLOBAL innodb_print_all_deadlocks = ON;
```

---

## 八、相關設定參數

```bash
# 一次查看所有 lock 相關參數
kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- \
  mariadb -u root -p"$(kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- printenv MARIADB_ROOT_PASSWORD)" \
  -e "SELECT @@innodb_lock_wait_timeout AS lock_wait_timeout,
             @@innodb_print_all_deadlocks AS print_all_deadlocks,
             @@innodb_deadlock_detect AS deadlock_detect,
             @@tx_isolation AS tx_isolation\G"
```

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `innodb_lock_wait_timeout` | 50 | Lock wait 超時秒數。超過後 waiting transaction 收到 ERROR 1205 |
| `innodb_print_all_deadlocks` | OFF | 是否將所有 deadlock 寫入 error log |
| `innodb_deadlock_detect` | ON | 是否啟用 deadlock detection。關閉後 deadlock 只能靠 timeout 解除 |
| `tx_isolation` | REPEATABLE-READ | Transaction isolation level。READ-COMMITTED 不使用 gap lock，可減少 lock 衝突 |

---

## 九、Metadata Lock（MDL）補充

Row lock 是 InnoDB 層的鎖。MariaDB 還有 **Server 層的 Metadata Lock (MDL)**，常被混淆。

| 項目 | Row Lock | Metadata Lock |
|------|----------|--------------|
| 層級 | InnoDB Storage Engine | MariaDB Server |
| 鎖什麼 | 行資料 | 表結構（DDL 操作） |
| 常見觸發 | DML（UPDATE/DELETE/INSERT） | DDL（ALTER TABLE）、LOCK TABLES |
| 監控表 | `information_schema.INNODB_LOCKS` | `information_schema.METADATA_LOCK_INFO`（需 plugin） |
| 典型症狀 | `LOCK WAIT` in INNODB_TRX | `Waiting for table metadata lock` in PROCESSLIST |

### 啟用 MDL 監控（如果需要）

```sql
-- 檢查 plugin 是否已安裝
SELECT * FROM information_schema.PLUGINS WHERE PLUGIN_NAME = 'METADATA_LOCK_INFO';

-- 安裝
INSTALL SONAME 'metadata_lock_info';

-- 查看 MDL
SELECT * FROM information_schema.METADATA_LOCK_INFO;
```

---

## 十、事後追溯（2-3 小時前的 Row Lock）

`INNODB_LOCK_WAITS` 只記錄**當下正在發生**的 lock wait，lock 結束後資料就消失了。
要追溯幾小時前的 row lock，需要靠以下來源：

### 現實：MariaDB 沒有 lock 歷史表

```
即時診斷（lock 正在發生）         事後追溯（lock 已結束）
  INNODB_LOCK_WAITS ✓               INNODB_LOCK_WAITS ✗ （資料已消失）
  INNODB_TRX ✓                      INNODB_TRX ✗
  SHOW ENGINE INNODB STATUS ✓       需要靠其他來源 ↓
```

### 方法 1：Slow Query Log 的 Lock_time（最實用）

每筆 slow query 都記錄了 `Lock_time` — 等待 row lock 的時間。如果一個查詢因為 lock wait 而變慢，
它會出現在 slow log 中，且 `Lock_time` > 0。

```bash
# 用 show_slow_queries.sh 看最近 1 天的慢查詢（見 05-slow-query-log-scripts.md）
bash show_slow_queries.sh kind-mdb default mariadb-chaos --days 1

# 輸出中找 Lock_time 高的記錄：
# [3] 2026-03-25 14:23:45 | Query_time: 5.234s | Lock: 5.001s | Rows exam/sent: 1/0
#     Schema: myapp | User: app_user
#     UPDATE orders SET status='cancel' WHERE id=5
#                                          ^^^^^^^^
#                                          Lock: 5.001s 表示這條 SQL 等了 5 秒才拿到 lock
```

**限制**：
- 只有 `Query_time` > `long_query_time`（通常 1s）的查詢才會記錄
- 如果 lock wait 時間短（< 1s），不會出現在 slow log
- `Lock_time` 記錄的是 **waiting** 方的等待時間，不直接顯示 **blocking** 方是誰

**判讀技巧**：
- `Lock_time` 接近 `Query_time` → 查詢本身不慢，完全是在等 lock
- `Lock_time` ≈ 50s（`innodb_lock_wait_timeout` 預設值）→ 等到 timeout 才放棄
- 同一時間點多筆 SQL 都有高 `Lock_time` → 同一個 blocker 卡住多個 transaction

### 方法 2：Performance Schema Digest 統計

`events_statements_summary_by_digest` 累積了每種 SQL pattern 的 lock 等待時間。

#### 2a. Digest Summary — 看 SQL pattern 的 lock 統計 + 時間範圍

這張表有 `FIRST_SEEN` 和 `LAST_SEEN` 欄位，可以知道該 SQL pattern **最早和最近一次**的執行時間：

```sql
SELECT
  SCHEMA_NAME,
  LEFT(DIGEST_TEXT, 80) AS digest_text,
  COUNT_STAR AS exec_count,
  ROUND(SUM_LOCK_TIME / 1000000, 3) AS total_lock_time_sec,
  ROUND(SUM_LOCK_TIME / COUNT_STAR / 1000000, 3) AS avg_lock_time_sec,
  FIRST_SEEN,
  LAST_SEEN
FROM performance_schema.events_statements_summary_by_digest
WHERE SUM_LOCK_TIME > 0
ORDER BY SUM_LOCK_TIME DESC
LIMIT 10\G
```

**解讀範例**：

```
digest_text: UPDATE `orders` SET `status` = ? WHERE `id` = ?
exec_count: 1234
total_lock_time_sec: 125.500
avg_lock_time_sec: 0.102
FIRST_SEEN: 2026-03-20 08:00:00
LAST_SEEN: 2026-03-25 14:23:45      ← 最近一次執行時間
```

- `LAST_SEEN` 在 2-3 小時前 → 這個 SQL pattern 那時候有執行
- `avg_lock_time_sec` > 0.1 → 平均每次執行都有明顯 lock 等待
- 但 `FIRST_SEEN`/`LAST_SEEN` 只告訴你**最早和最晚**，中間的個別執行時間看不到

> **注意**：`SUM_LOCK_TIME` 是 picoseconds（10^-12 秒），需除以 1,000,000 轉成 microseconds 再除以 1,000,000 轉成秒。
> 實務上 MariaDB 的精度為 microseconds，所以除以 1,000,000 即可。
> 這是自 server 啟動（或上次 `TRUNCATE TABLE`）以來的**累積值**。

#### 2b. History Long — 看個別 SQL 的執行時間（有時間戳）

如果需要**個別 SQL 的精確執行時間**，用 `events_statements_history_long`：

```sql
SELECT
  DATE_SUB(NOW(), INTERVAL (
    SELECT VARIABLE_VALUE FROM information_schema.GLOBAL_STATUS
    WHERE VARIABLE_NAME = 'UPTIME'
  ) - TIMER_START / 1000000000000 SECOND) AS approx_time,
  sql_text,
  ROUND(LOCK_TIME / 1000000, 3) AS lock_time_sec,
  ROUND(TIMER_WAIT / 1000000000, 3) AS query_time_ms,
  ROWS_EXAMINED,
  ROWS_SENT
FROM performance_schema.events_statements_history_long
WHERE LOCK_TIME > 0
ORDER BY LOCK_TIME DESC
LIMIT 20\G
```

> **限制**：`events_statements_history_long` 是 ring buffer，預設保留最近 10,000 條 SQL（所有 thread 共享）。
> 在高流量下，2-3 小時前的 SQL 很可能已被覆蓋。

查看 buffer 大小：

```sql
SELECT @@performance_schema_events_statements_history_long_size;
-- 預設 10000。如果需要更長的歷史，可在 my.cnf 中調大。
```

#### 2c. 定期快照 — 定位特定時間段的 lock 變化

如果想精確知道某段時間的 lock 狀況，可以用「重置 → 等待 → 查詢」的方式：

```sql
-- 重置統計（小心：會清除所有 digest 統計）
TRUNCATE TABLE performance_schema.events_statements_summary_by_digest;

-- 等待一段時間後再查，就能看到這段時間內的 lock 統計
-- （此時 FIRST_SEEN/LAST_SEEN 就是這段時間內的範圍）
```

#### 時間資訊比較

| 來源 | 有時間戳？ | 精確度 | 保留多久 |
|------|-----------|--------|---------|
| `summary_by_digest.FIRST_SEEN` | 有 | 只有最早和最晚 | Server 啟動至今 |
| `summary_by_digest.LAST_SEEN` | 有 | 只有最早和最晚 | Server 啟動至今 |
| `history_long.TIMER_START` | 有（需轉換） | 每條 SQL 都有 | Ring buffer，高流量下很快覆蓋 |
| Slow log `# Time:` | 有 | 每條 SQL 都有 | 直到 log rotation |

### 方法 3：Error Log 中的 Deadlock 記錄

如果之前已開啟 `innodb_print_all_deadlocks = ON`（見第八節），所有 deadlock 都會寫入 error log：

```bash
# 查看 MariaDB error log 中的 deadlock 記錄
kubectl --context=kind-mdb -n default logs mariadb-chaos-0 -c mariadb | grep -A 30 "DEADLOCK"
```

> 僅適用於 deadlock。一般的 lock wait（timeout）不會記錄在 error log 中。

### 方法比較

| 方法 | 能看到什麼 | 限制 | 適用場景 |
|------|-----------|------|---------|
| Slow log Lock_time | 哪些 SQL 因 lock 等待而變慢 | 只記錄 > long_query_time 的查詢 | 事後追溯最實用 |
| PS digest summary | 哪些 SQL pattern 累積最多 lock 時間 | 累積值，無法定位精確時間點 | 找出反覆有 lock 問題的 SQL |
| Error log deadlock | Deadlock 的完整詳情 | 只有 deadlock，需預先開啟設定 | Deadlock 事後分析 |
| INNODB_LOCK_WAITS | 完整的 blocker/waiter 資訊 | **只有即時**，lock 結束就消失 | 即時診斷 |

### 建議的事前準備

為了讓事後追溯更有效，建議預先設定：

```sql
-- 1. 確保 slow log 開啟（MariaDB Operator 通常已設定）
SELECT @@slow_query_log, @@long_query_time;

-- 2. 開啟 deadlock 記錄
SET GLOBAL innodb_print_all_deadlocks = ON;

-- 3. 確認 Performance Schema 啟用
SELECT @@performance_schema;
```

---

## 十一、Prometheus Metrics 與 SQL 診斷的對接

### Prometheus 只告訴你「有問題」，不告訴你「哪條 SQL」

mysqld_exporter 匯出的 lock 相關 metrics 來自 `SHOW GLOBAL STATUS`，只有**計數器和統計值**，沒有 SQL 內容：

```
Prometheus metric                                     來源（MariaDB GLOBAL STATUS）
─────────────────────────────────────────────────────────────────────────────────
mysql_global_status_innodb_row_lock_current_waits  →  Innodb_row_lock_current_waits（當前等待數）
mysql_global_status_innodb_row_lock_time_total      →  Innodb_row_lock_time（累計等待時間 ms）
mysql_global_status_innodb_row_lock_time_avg        →  Innodb_row_lock_time_avg（平均等待時間 ms）
mysql_global_status_innodb_row_lock_time_max        →  Innodb_row_lock_time_max（最大等待時間 ms）
mysql_global_status_innodb_row_lock_waits_total     →  Innodb_row_lock_waits（累計等待次數）
```

### 從 Prometheus Alert 到找出 SQL 的完整流程

```
Prometheus alert: mysql_global_status_innodb_row_lock_current_waits > 0
    │
    │  ← 這只告訴你「現在有 N 個 transaction 在等 row lock」
    │     不告訴你是哪個 table、哪條 SQL、誰 block 誰
    │
    ▼
進 MariaDB 查（第三節 Query 1）：
    SELECT ... FROM INNODB_LOCK_WAITS w
    JOIN INNODB_TRX b ... JOIN INNODB_TRX r ... JOIN INNODB_LOCKS l ...
    │
    ▼
得到完整資訊：
    - waiting_query:  等待中的 SQL
    - blocking_query: 持有 lock 的 SQL（可能 NULL → 第五節）
    - locked_table:   被鎖的表
    - lock_data:      被鎖的行 PK
    - blocking_age:   blocker 已跑多久
```

### 實際操作：Alert 觸發時怎麼做

**Step 1**：確認哪個 pod 有 lock wait（如果有多個 instance）

```bash
# 如果 Prometheus alert 有 instance label，直接知道是哪個 pod
# 否則用腳本掃所有 pod：
bash check_row_locks.sh kind-mdb default mariadb-chaos
```

**Step 2**：在有 lock wait 的 pod 上執行診斷 SQL

```bash
MYSQL_PWD=$(kubectl --context=kind-mdb -n default \
  exec mariadb-chaos-0 -c mariadb -- printenv MARIADB_ROOT_PASSWORD)

# 查看當前 lock wait 詳情（第三節 Query 1）
kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- \
  mariadb -u root -p"${MYSQL_PWD}" -e "
    SELECT
      r.trx_id              AS waiting_trx_id,
      r.trx_mysql_thread_id AS waiting_thread,
      r.trx_query           AS waiting_query,
      b.trx_id              AS blocking_trx_id,
      b.trx_mysql_thread_id AS blocking_thread,
      b.trx_query           AS blocking_query,
      TIMESTAMPDIFF(SECOND, b.trx_started, NOW()) AS blocking_age_sec,
      l.lock_table          AS locked_table,
      l.lock_mode           AS lock_mode,
      l.lock_data           AS lock_data
    FROM information_schema.INNODB_LOCK_WAITS w
    JOIN information_schema.INNODB_TRX b ON b.trx_id = w.blocking_trx_id
    JOIN information_schema.INNODB_TRX r ON r.trx_id = w.requesting_trx_id
    JOIN information_schema.INNODB_LOCKS l ON l.lock_id = w.requested_lock_id\G"
```

**Step 3**：如果 `blocking_query` 為 NULL → 第五節追溯方法

**Step 4**：如果 alert 已 resolved（lock 已結束）→ 第十節事後追溯

### 常用 PromQL 告警規則參考

```yaml
# 當前有 row lock wait
- alert: MariaDBRowLockWait
  expr: mysql_global_status_innodb_row_lock_current_waits > 0
  for: 30s
  labels:
    severity: warning
  annotations:
    summary: "Row lock wait detected on {{ $labels.instance }}"
    runbook: "執行 check_row_locks.sh 或第三節 Query 1 找出 blocking SQL"

# lock wait 次數快速增長（5 分鐘內增加 > 10 次）
- alert: MariaDBRowLockWaitSpike
  expr: increase(mysql_global_status_innodb_row_lock_waits_total[5m]) > 10
  labels:
    severity: warning
  annotations:
    summary: "Row lock wait spike on {{ $labels.instance }}"

# 平均 lock wait 時間過高
- alert: MariaDBRowLockTimeHigh
  expr: mysql_global_status_innodb_row_lock_time_avg > 5000
  labels:
    severity: critical
  annotations:
    summary: "Average row lock wait > 5s on {{ $labels.instance }}"
```

---

## 十二、診斷流程總結

```
發現問題
  ├─ Prometheus: mysql_global_status_innodb_row_lock_current_waits > 0
  ├─ Slow query log Lock_time > 0.1s（見 08-how-to-read-slow-query-output.md）
  ├─ 應用端收到 ERROR 1205（lock wait timeout）
  └─ 應用端收到 ERROR 1213（deadlock）
     │
     ▼
執行 check_row_locks.sh kind-mdb default mariadb-chaos
     │
     ├─ 有 lock wait
     │    ├─ blocking_query 有值 → 直接看是什麼 SQL，評估是否需要 KILL
     │    └─ blocking_query = NULL → 查 events_statements_history 追溯歷史 SQL
     │         └─ 通常是 AP 開了 transaction 做完 SQL 沒 commit（PROCESSLIST 顯示 Sleep）
     │
     ├─ 無 lock wait 但有長 transaction（> 30s）
     │    └─ 潛在風險 — 通知 AP 團隊檢查是否有忘記 commit 的 transaction
     │
     ├─ ERROR 1213（deadlock）
     │    └─ SHOW ENGINE INNODB STATUS 查 LATEST DETECTED DEADLOCK
     │         └─ 優化 SQL 執行順序避免再發生
     │
     ├─ 全部正常（lock 已結束）
     │    └─ 事後追溯 → 第十節
     │         ├─ Slow log Lock_time 找等 lock 的 SQL
     │         ├─ PS digest summary 找反覆有 lock 的 SQL pattern
     │         └─ Error log 找 deadlock 記錄
     │
     └─ 預防
          └─ 確認 slow log ON + innodb_print_all_deadlocks ON
```
