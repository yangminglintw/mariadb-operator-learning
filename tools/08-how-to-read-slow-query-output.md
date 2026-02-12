# Slow Query 輸出解讀指南

本文件說明如何解讀 `show_slow_queries.sh` 腳本的輸出，幫助你快速找出有問題的慢查詢。

> 腳本原始碼與設計說明見 `05-slow-query-log-scripts.md`

---

## 快速判斷流程

```
先用 --days 1 看近況
    │
    ▼
結果太多？（數十～數百筆）
    │
    ├─ YES → 用 --top 10 篩出最慢的
    │
    └─ NO  → 直接逐筆看
              │
              ▼
        看 Rows exam/sent 比值
              │
              ├─ > 100  → 效率差，可能要加 Index
              │            → 下一步：analyze_sql.sh 做 EXPLAIN
              │
              ├─ 10~100 → 可接受，但可優化
              │
              └─ < 10   → 正常
                          │
                          ▼
                    想看這個 SQL 在其他 Pod 的歷史？
                          → get_sql_by_digest.sh
```

---

## 參數用法

本指南只涵蓋兩個最常用參數：

| 參數 | 說明 | 範例 |
|------|------|------|
| `--days <N>` | 查詢最近 N 天的慢查詢（預設：1） | `--days 1`、`--days 7`、`--days 14` |
| `--top <N>` | 依 Query_time 排序，只顯示最慢的 N 筆 | `--top 10` |

**組合使用**：`--days 7 --top 10` 表示「最近 7 天，取最慢 10 筆」。

```bash
# 看最近 1 天所有慢查詢
./show_slow_queries.sh kind-mdb default mariadb-chaos --days 1

# 最近 7 天，只看最慢 5 筆
./show_slow_queries.sh kind-mdb default mariadb-chaos --days 7 --top 5
```

---

## 預設模式輸出（`--days N`）

不加 `--top` 時，腳本以時間順序列出所有慢查詢。

### 輸出範例

```
=== mariadb-0 (Primary) === [log: 15M, 12345 lines, last 1 days]

[1] 2025-02-10 14:30:00 | Query_time: 2.346s | Lock: 0.000s | Rows exam/sent: 50000/1
    Schema: myapp | User: app_user
    SELECT * FROM users WHERE email = 'test@test.com'

[2] 2025-02-10 14:29:55 | Query_time: 1.234s | Lock: 0.001s | Rows exam/sent: 30000/10
    Schema: myapp | User: app_user
    SELECT * FROM orders WHERE created_at > '2025-01-01' AND status = 'pending'

--- 2 queries found (showing all) ---
```

### Header 解讀

```
=== mariadb-0 (Primary) === [log: 15M, 12345 lines, last 1 days]
     │          │                  │     │              │
     Pod 名稱   角色               檔案   log 總行數     查詢的時間範圍
                Primary=可寫主庫   大小
                Replica=唯讀副本
```

### 每筆 Entry 格式

每筆慢查詢固定三行：

```
[序號] 時間 | Query_time: Xs | Lock: Xs | Rows exam/sent: N/N    ← 第一行：數據
    Schema: xxx | User: xxx                                       ← 第二行：來源
    SQL 全文                                                      ← 第三行：SQL
```

### 欄位說明

| 欄位 | 說明 | 範例 |
|------|------|------|
| `[序號]` | 該 Pod 內的慢查詢流水號 | `[1]`、`[2]` |
| 時間 | SQL 執行的時間（UTC） | `2025-02-10 14:30:00` |
| `Query_time` | SQL 執行耗時（秒） | `2.346s` |
| `Lock` | 等待鎖的時間（秒） | `0.000s` |
| `Rows exam` | 資料庫掃描了多少行 | `50000` |
| `Rows sent` | 實際回傳給 client 多少行 | `1` |
| `Schema` | SQL 執行的 database 名稱 | `myapp` |
| `User` | 執行該 SQL 的使用者 | `app_user` |
| SQL 全文 | 完整的 SQL（含實際參數值） | `SELECT * FROM users WHERE ...` |

### Footer 解讀

```
--- 2 queries found (showing all) ---
     │                    │
     時間範圍內的慢查詢總數  表示未截斷，顯示全部
```

### 判斷重點

| 觀察 | 意義 | 建議動作 |
|------|------|----------|
| `Query_time` > 5s | 嚴重慢查詢 | 立即分析，可能需要加 Index |
| `Query_time` 1~5s | 中等慢查詢 | 應該優化 |
| `Query_time` < 1s | 輕微慢查詢 | 看頻率，偶爾可忽略 |
| `Rows exam/sent` 比值 > 100 | 掃描效率差 | 需要加 Index（見掃描倍率表） |
| `Lock` > 0.1s | 鎖等待明顯 | 檢查是否有 Lock 競爭 |
| 同一個 SQL 反覆出現 | 高頻慢查詢 | 優先處理，用 `--top` 確認排名 |

---

## Top N 模式輸出（`--days N --top N`）

加了 `--top` 後，腳本收集所有慢查詢，按 `Query_time` 由大到小排序，以表格格式輸出。

### 輸出範例

```
=== mariadb-0 (Primary) === [log: 15M, 12345 lines, last 7 days, showing top 5]

Rank | Query_time | Lock_time | Rows exam/sent | Schema     | SQL (truncated)
   1 |     5.234s |    0.012s |   200000/1     | myapp      | SELECT * FROM orders WHERE ...
   2 |     2.346s |    0.000s |    50000/1     | myapp      | SELECT * FROM users WHERE e...
   3 |     1.890s |    0.000s |    30000/50    | myapp      | SELECT o.*, u.name FROM ord...

--- 128 queries in period, showing top 5 ---
```

### Header 差異

與預設模式相比，Header 多了 `showing top N`：

```
=== mariadb-0 (Primary) === [log: 15M, 12345 lines, last 7 days, showing top 5]
                                                                          │
                                                                    多了這個資訊
```

### 欄位說明

| 欄位 | 說明 | 範例 |
|------|------|------|
| `Rank` | 依 Query_time 排序的名次（1 = 最慢） | `1` |
| `Query_time` | SQL 執行耗時（秒） | `5.234s` |
| `Lock_time` | 等待鎖的時間（秒） | `0.012s` |
| `Rows exam/sent` | 掃描行數 / 回傳行數 | `200000/1` |
| `Schema` | 執行的 database 名稱 | `myapp` |
| `SQL (truncated)` | SQL 前 60 字元 + `...`（超過時截斷） | `SELECT * FROM orders WHERE ...` |

> **注意**：Top N 模式的 SQL 被截斷到 60 字元。如果需要看完整 SQL，改用不加 `--top` 的預設模式。

### Footer 解讀

```
--- 128 queries in period, showing top 5 ---
      │                           │
      時間範圍內的慢查詢總數         實際顯示筆數
```

---

## 判斷門檻速查表

### Query_time 門檻

| Query_time | 嚴重度 | 說明 |
|------------|--------|------|
| < 1s | 低 | 剛超過 `long_query_time` 門檻，看頻率決定是否處理 |
| 1~5s | 中 | 使用者可能已感受到延遲，建議優化 |
| 5~10s | 高 | 明顯影響體驗，應優先處理 |
| > 10s | 緊急 | 可能造成連線堆積，必須立即處理 |

### 掃描倍率（Rows examined / Rows sent）

```
掃描倍率 = Rows examined ÷ Rows sent

掃描倍率    結論
─────────────────────────────
< 10        正常
10 ~ 100    可接受，但可優化
> 100       效率差，建議加 Index
> 1000      嚴重問題，必須處理
```

**範例**：
- `Rows exam/sent: 50000/1` → 倍率 = 50000，嚴重問題
- `Rows exam/sent: 30000/10` → 倍率 = 3000，嚴重問題
- `Rows exam/sent: 100/50` → 倍率 = 2，正常

> 這與 `get_sql_by_digest.sh --verbose` 中的 `rows_examined / rows_sent` 是同一個概念，詳見 `06-how-to-read-sql-diagnosis-output.md`。

### Lock_time 判斷

| Lock_time | 意義 |
|-----------|------|
| 0.000s | 正常，沒有鎖等待 |
| 0.001~0.1s | 輕微鎖等待，通常可忽略 |
| > 0.1s | 明顯鎖競爭，需檢查是否有長 transaction 或 table lock |
| > 1s | 嚴重鎖等待，可能有 deadlock 或 DDL 阻塞 |

---

## 下一步動作

| 發現 | 下一步 | 指令 |
|------|--------|------|
| 找到可疑的慢 SQL | 用 `analyze_sql.sh` 做 EXPLAIN 分析 | `./analyze_sql.sh kind-mdb default mariadb-chaos "SELECT ..."` |
| 想看這個 SQL 在所有 Pod 的執行統計 | 用 `get_sql_by_digest.sh` 查 digest | `./get_sql_by_digest.sh kind-mdb default mariadb-chaos <digest>` |
| 想看 EXPLAIN 和 Index 建議 | 用 `analyze_sql.sh` | 見 `03-sql-tuning-guide.md` |

**串接流程**：

```
show_slow_queries.sh（找到慢 SQL）
    │
    ├─→ analyze_sql.sh（EXPLAIN 分析 + Index 建議）
    │
    └─→ get_sql_by_digest.sh（跨 Pod 執行統計）
```

---

## FAQ

### Q: 為什麼 Replica 也有 slow query？

**A:** 這是正常的。Replica 會記錄慢查詢的原因：
- **讀取流量**：MaxScale 把 SELECT 分散到 Replica，如果 SELECT 本身慢就會記錄
- **Replication replay**：Primary 的大 transaction 在 Replica 上 replay 時，如果超過 `long_query_time` 也會記錄

### Q: `--- No slow queries found ---` 代表什麼？

**A:** 代表在指定的時間範圍內，該 Pod 的 slow query log 中沒有符合條件的記錄。可能原因：
- 真的沒有慢查詢（好事）
- `long_query_time` 設太高，慢查詢沒達到門檻
- Log 已 rotate，舊記錄不在了

### Q: `--days 1` 和 `--top 10` 可以一起用嗎？

**A:** 可以。`--days 1 --top 10` 表示「從最近 1 天的慢查詢中，取最慢的 10 筆」。這是最常用的組合，先限制時間範圍再取 Top N。

### Q: Top N 模式的 SQL 被截斷看不到完整內容怎麼辦？

**A:** Top N 模式只顯示 SQL 前 60 字元。要看完整 SQL，用預設模式重新查：
```bash
# 先用 top 找到最慢的
./show_slow_queries.sh kind-mdb default mariadb-chaos --days 7 --top 5

# 再用預設模式看完整 SQL
./show_slow_queries.sh kind-mdb default mariadb-chaos --days 7
```

### Q: 為什麼 Header 顯示 `[slow log file not found]`？

**A:** 代表無法從該 Pod 取得 slow query log 的路徑。可能原因：
- Slow query log 未啟用（`slow_query_log = OFF`）
- Pod 尚未 ready
- 容器內 MariaDB 連線失敗
