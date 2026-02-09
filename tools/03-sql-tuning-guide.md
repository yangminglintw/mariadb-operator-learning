# SQL Tuning 指南

## Index 設計原則

### Leftmost Prefix Rule（複合 Index）

```
Index: (a, b, c)

可以用：
  WHERE a = 1
  WHERE a = 1 AND b = 2
  WHERE a = 1 AND b = 2 AND c = 3

不能用：
  WHERE b = 2              -- 跳過 a
  WHERE a = 1 AND c = 3    -- 跳過 b，只能用 a
```

### 設計原則

| 原則 | 說明 |
|------|------|
| 高 Cardinality 放前面 | user_id（100萬種）比 status（3種）優先 |
| 範圍查詢放最後 | (status, created_at) 而不是 (created_at, status) |
| Covering Index | 包含所有 SELECT 欄位，不用回 table |

---

## 常見 Anti-Patterns

### 1. 對欄位使用 Function

```sql
-- 不會用 index
WHERE YEAR(created_at) = 2024
WHERE DATE(created_at) = '2024-01-01'
WHERE LOWER(email) = 'test@test.com'

-- 會用 index
WHERE created_at >= '2024-01-01' AND created_at < '2025-01-01'
WHERE created_at >= '2024-01-01' AND created_at < '2024-01-02'
WHERE email = 'test@test.com'  -- 存入時就轉小寫
```

### 2. 隱式型別轉換

```sql
-- phone 是 VARCHAR
-- 不會用 index（MySQL 要轉換每一筆）
WHERE phone = 0912345678

-- 會用 index
WHERE phone = '0912345678'
```

### 3. LIKE 開頭 %

```sql
-- 不會用 index
WHERE email LIKE '%@gmail.com'

-- 會用 index
WHERE email LIKE 'test%'

-- 替代方案：
-- 1. 加 email_domain 欄位 + index
-- 2. 用 Full-Text Search
-- 3. 加 reversed 欄位
```

### 4. OR 條件（不同欄位）

```sql
-- 效率差（需要兩個 index + index_merge）
WHERE name = 'John' OR email = 'john@test.com'

-- 改用 UNION
SELECT * FROM users WHERE name = 'John'
UNION
SELECT * FROM users WHERE email = 'john@test.com'
```

### 5. NOT IN / != / <>

```sql
-- 通常不能用 index
WHERE status != 'deleted'
WHERE id NOT IN (1, 2, 3)

-- 如果可以，改成明確列出
WHERE status IN ('active', 'pending')
```

---

## SELECT 優化

### 避免 SELECT *

```sql
-- 不好
SELECT * FROM users WHERE id = 1;

-- 好
SELECT id, name, email FROM users WHERE id = 1;

-- 原因：
-- 1. 讀取不需要的欄位
-- 2. 無法使用 Covering Index
-- 3. 網路傳輸浪費
```

### 避免 COUNT(*) 大表

```sql
-- 慢（InnoDB 要掃全表）
SELECT COUNT(*) FROM big_table;

-- 替代：用預估值
SELECT TABLE_ROWS FROM information_schema.TABLES
WHERE TABLE_NAME = 'big_table';

-- 或維護 counter table
```

### 避免大 OFFSET

```sql
-- 慢（要掃 100 萬筆再丟掉）
SELECT * FROM users ORDER BY id LIMIT 1000000, 10;

-- 用 cursor-based pagination
SELECT * FROM users WHERE id > 1000000 ORDER BY id LIMIT 10;
```

---

## JOIN 優化

### 原則

- 小表 JOIN 大表（小表當驅動表）
- JOIN 欄位要有 Index
- JOIN 欄位型別要一致

### 範例

```sql
SELECT * FROM orders o
JOIN users u ON o.user_id = u.id
WHERE o.created_at > '2024-01-01';

-- 確保：
-- 1. orders.user_id 有 index
-- 2. users.id 有 index（PK 已有）
-- 3. orders.created_at 有 index
-- 4. 考慮複合 index: (created_at, user_id)
```

---

## 實用診斷 SQL

### 找最慢的 Query

```sql
SELECT
    DIGEST_TEXT,
    COUNT_STAR AS exec_count,
    ROUND(SUM_TIMER_WAIT/1000000000000, 2) AS total_time_sec,
    ROUND(AVG_TIMER_WAIT/1000000000, 2) AS avg_time_ms,
    SUM_ROWS_EXAMINED AS rows_examined
FROM performance_schema.events_statements_summary_by_digest
WHERE SCHEMA_NAME NOT IN ('mysql', 'performance_schema', 'sys')
ORDER BY SUM_TIMER_WAIT DESC
LIMIT 10;
```

### 找效率差的 Query（rows_examined >> rows_sent）

```sql
SELECT
    DIGEST_TEXT,
    SUM_ROWS_EXAMINED,
    SUM_ROWS_SENT,
    ROUND(SUM_ROWS_EXAMINED / NULLIF(SUM_ROWS_SENT, 0), 0) AS ratio
FROM performance_schema.events_statements_summary_by_digest
WHERE SUM_ROWS_SENT > 0
ORDER BY ratio DESC
LIMIT 10;
```

### 找沒用到的 Index

```sql
SELECT
    object_schema,
    object_name AS table_name,
    index_name
FROM performance_schema.table_io_waits_summary_by_index_usage
WHERE index_name IS NOT NULL
  AND count_read = 0
  AND object_schema NOT IN ('mysql', 'performance_schema')
ORDER BY object_schema, object_name;
```

### 找 Table 大小

```sql
SELECT
    TABLE_NAME,
    TABLE_ROWS,
    ROUND(DATA_LENGTH / 1024 / 1024, 2) AS data_mb,
    ROUND(INDEX_LENGTH / 1024 / 1024, 2) AS index_mb
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'your_db'
ORDER BY DATA_LENGTH DESC;
```

---

## 什麼時候不需要優化

| 情況 | 原因 |
|------|------|
| 小 Table（< 1000 筆） | Full Scan 可能比 Index 更快 |
| 低頻率 Query（一天幾次） | 優化 ROI 太低 |
| 已經夠快（< 100ms） | 除非是高頻率 query |
| Batch Job / 報表 | 可以接受慢，用 Replica 跑 |
| Optimizer 選擇 Full Scan | 取 > 30% 資料時合理，加 whitelist |

---

## Index 的代價

```
每個 Index 的代價：
├── 磁碟空間
├── INSERT 變慢（要更新所有 index）
├── UPDATE 變慢（如果更新 indexed 欄位）
└── DELETE 變慢（要更新所有 index）

建議：
├── 不要盲目加 index
├── 定期檢查沒用到的 index，考慮刪除
└── Write-heavy table 要謹慎加 index
```

---

## Cheat Sheet

```
+------------------------------+--------------------------------+
|           DO                 |           DON'T                |
+------------------------------+--------------------------------+
| SELECT 需要的欄位            | SELECT *                       |
| WHERE created_at > '2024...' | WHERE YEAR(created_at) = 2024  |
| WHERE phone = '0912345678'   | WHERE phone = 0912345678       |
| WHERE email LIKE 'test%'     | WHERE email LIKE '%test%'      |
| WHERE id > 1000 LIMIT 10     | LIMIT 1000, 10                 |
| WHERE status IN ('a','b')    | WHERE status != 'c'            |
+------------------------------+--------------------------------+

EXPLAIN 檢查重點：
+------------------+---------------------------+
| 好               | 不好                      |
+------------------+---------------------------+
| type: const/ref  | type: ALL (Full Scan)     |
| key: 有值        | key: NULL                 |
| rows: 小         | rows: 很大                |
+------------------+---------------------------+
```
