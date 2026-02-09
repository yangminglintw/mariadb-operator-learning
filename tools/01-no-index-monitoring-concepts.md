# No-Index Query 監控概念

## 問題背景

```
目標：確保 AP 的 SQL 都有使用 Index
流程：Prometheus metrics → 告警 → AP 開發者查完整 SQL → 優化
問題：Prometheus 只有 digest，AP 需要完整 SQL
```

---

## Performance Schema 核心概念

### 關鍵 Tables

| Table | 類型 | SQL_TEXT | 保留時間 | 用途 |
|-------|------|----------|----------|------|
| `events_statements_current` | 明細 | ✓ | 即時（執行中） | 看正在跑的 SQL |
| `events_statements_history` | 明細 | ✓ | 短（每 thread 10 條） | 查完整 SQL |
| `events_statements_history_long` | 明細 | ✓ | 中（全域 10000 條） | 比 history 多 |
| `events_statements_summary_by_digest` | 彙總 | ✗ | 永久 | Prometheus 用 |

### 其他 Summary Tables

| Table | 分組依據 | 用途 |
|-------|----------|------|
| `summary_by_account_by_event_name` | USER + HOST | 看哪個帳號執行最多 |
| `summary_by_host_by_event_name` | HOST | 看哪個 client IP 最多 |
| `summary_by_user_by_event_name` | USER | 看哪個 user 最多 |
| `summary_by_thread_by_event_name` | THREAD_ID | 看哪個 thread 最忙 |
| `summary_by_program` | SP/Function | 看哪個 SP 最慢 |
| `summary_global_by_event_name` | EVENT_NAME | 全域 SQL 類型統計 |

### Digest 概念

```
原始 SQL: SELECT * FROM users WHERE id = 123
           ↓ 標準化
Digest Text: SELECT * FROM users WHERE id = ?
           ↓ Hash
Digest: abc123def456...
```

### History vs Summary 差異

```
events_statements_history（明細）
├── 有 SQL_TEXT（完整 SQL，含參數）
├── 有 DIGEST（可關聯到 summary）
├── 保留時間短（每 thread 10 條）
└── 用途：查完整 SQL

events_statements_summary_by_digest（彙總）
├── 沒有 SQL_TEXT
├── 只有 DIGEST_TEXT（參數變成 ?）
├── 永久保留
└── 用途：Prometheus metrics
```

### 設定參數

```sql
SHOW VARIABLES LIKE 'performance_schema%statements%';

-- 重要參數：
-- performance_schema_events_statements_history_size = 10      (每 thread)
-- performance_schema_events_statements_history_long_size = 10000  (全域)
-- performance_schema_digests_size = 10000   (summary 最多幾種 digest)
-- performance_schema_max_sql_text_length = 1024  (SQL_TEXT 最大長度)
```

---

## Primary vs Replica

```
Performance Schema 是 Local 的！

Primary: 記錄 AP 的 INSERT/UPDATE/DELETE + SELECT（如果沒分流）
Replica: 記錄 Replication SQL + SELECT（如果有讀寫分離）

結論：查所有 Pods 才能確保找到完整 SQL
```

---

## Metrics 是累計值

### 重要觀念

```
SUM_NO_INDEX_USED 是累計值：
├── 過去沒用 index 的執行次數還在
├── 加了 index 之後，新的執行不會再增加
└── 但舊的數字不會減少

正確看法：看 rate()（速率）而不是總量
```

### Prometheus Query

```promql
# 錯誤：看總量
mysql_perf_schema_events_statements_no_index_used_total

# 正確：看速率
rate(mysql_perf_schema_events_statements_no_index_used_total[5m])

# rate = 0 表示問題已修復
# rate > 0 表示還有執行沒用 index
```

---

## 有 Index 但沒用到的情況

### 常見原因

| 原因 | 說明 | 是否需要修 |
|------|------|-----------|
| 資料量太少 | Full scan 更快 | 不需要 |
| 取太多資料 | > 30% 的 table | 不需要 |
| Cardinality 低 | 太多重複值 | 看情況 |
| Function 包住欄位 | WHERE YEAR(date) | 需要改 SQL |
| 隱式型別轉換 | VARCHAR vs INT | 需要改 SQL |
| LIKE 開頭 % | 無法用 index | 看情況 |
| 統計資訊過時 | Optimizer 判斷錯 | ANALYZE TABLE |

### 如何區分

```sql
-- 看 EXPLAIN
EXPLAIN SELECT * FROM users WHERE status = 'active';

-- 看 possible_keys 和 key
-- possible_keys 有值，key 是 NULL = Optimizer 選擇不用
-- 這種情況可能是正常的，加到 whitelist
```

---

## 回覆 User 範本

當 user 說「加了 index 但 metrics 還在計數」：

```
您好，

有兩種可能：

1. 歷史累計：metrics 是累計值，加 index 後新執行不會再增加，
   但舊數字還在。請看 Grafana 的 rate()，如果 rate=0 表示已修好。

2. Optimizer 選擇不用：即使有 index，如果 query 要取大部分資料，
   Optimizer 會選擇 Full Scan（這樣更快）。

   請跑：EXPLAIN <your SQL>

   如果 possible_keys 有您的 index，但 key 是 NULL，
   表示 Optimizer 認為 Full Scan 更有效率。
   這種情況不需要修，可以把這個 query 加到白名單。
```

---

## 相關資源

- [AWS CloudWatch Database Insights](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Database-Insights.html)
- [mysqld_exporter](https://github.com/prometheus/mysqld_exporter)
