# SQL 診斷輸出解讀指南

本文件說明如何解讀 `get_sql_by_digest.sh` 腳本的輸出，幫助你快速判斷 SQL 是否有效能問題。

---

## 快速判斷流程

```
拿到 Digest
    │
    ▼
執行 get_sql_by_digest.sh（精簡版）
    │
    ├─ no_index_count = total_exec？
    │      │
    │      ├─ YES → 從來沒用 Index，可能有問題
    │      │         → 下一步：執行 analyze_sql.sh 確認
    │      │
    │      └─ NO  → 有時用 Index、有時沒用
    │                → 下一步：用 --verbose 看細節
    │
    └─ 找不到 SQL？
           → SQL 已從 performance_schema 淘汰
           → 下一步：重新觸發該 SQL 後再查
```

---

## 精簡版輸出（預設）

### 輸出格式

```
Digest: abc123def456...

[mariadb-0] (Primary) SELECT * FROM users WHERE email = 'test@example.com'
[mariadb-2] (Replica) SELECT * FROM users WHERE email = 'foo@bar.com'

=== Digest Pattern (from summary) ===
[mariadb-0] (Primary)
*************************** 1. row ***************************
   schema_name: myapp
       pattern: SELECT * FROM `users` WHERE `email` = ?
no_index_count: 5000
    total_exec: 5000
```

### 欄位說明

| 欄位 | 說明 | 範例 |
|------|------|------|
| `Digest` | SQL 的唯一識別碼（hash） | `abc123def456...` |
| `[pod-name]` | 執行該 SQL 的 MariaDB pod | `mariadb-0` |
| `(Role)` | Pod 角色：Primary（可寫）或 Replica（唯讀） | `(Primary)` |
| `SQL_TEXT` | 完整 SQL（含實際參數值） | `SELECT * FROM users WHERE email = 'test@example.com'` |
| `schema_name` | SQL 執行的 database 名稱 | `myapp` |
| `pattern` | SQL 模板（參數用 `?` 取代） | `SELECT * FROM users WHERE email = ?` |
| `no_index_count` | 沒用 Index 的執行次數 | `5000` |
| `total_exec` | 總執行次數 | `5000` |

### 判斷重點

| 情況 | 意義 | 建議動作 |
|------|------|----------|
| `no_index_count` = `total_exec` | 這個 SQL **從來沒用過 Index** | 需要分析，可能要加 Index |
| `no_index_count` < `total_exec` | 有時用 Index、有時沒用 | 用 `--verbose` 進一步分析 |
| `no_index_count` = 0 | 每次都有用 Index | 正常，不需處理 |
| Role = `Primary` | 這是 Write query 或指定打 Primary | 確認是否應該打 Replica |
| Role = `Replica` | 這是 Read query | 正常 |

---

## 完整版輸出（--verbose）

### 輸出格式

```
Digest: abc123def456...

----------------------------------------
Pod: mariadb-0
Role: Primary
[Found in history]
*************************** 1. row ***************************
  schema_name: myapp
     sql_text: SELECT * FROM users WHERE email = 'test@test.com'
rows_examined: 50000
    rows_sent: 1

----------------------------------------
Pod: mariadb-1
Role: Replica
[Found in history_long]
*************************** 1. row ***************************
  schema_name: myapp
     sql_text: SELECT * FROM users WHERE email = 'another@test.com'
rows_examined: 50000
    rows_sent: 1

----------------------------------------
Pod: mariadb-2
Role: Replica
[Not found in history]

=== Digest Pattern (from summary) ===
...
```

### 額外欄位說明

| 欄位 | 說明 | 範例 |
|------|------|------|
| `rows_examined` | 資料庫掃描了多少行 | `50000` |
| `rows_sent` | 實際回傳給 client 多少行 | `1` |
| `[Found in history]` | 最近執行過（還在 per-thread buffer） | - |
| `[Found in history_long]` | 稍早執行過（在 global buffer） | - |
| `[Not found in history]` | 這個 pod 沒有執行過此 SQL | - |

### 效率判斷

```
效率公式：rows_examined / rows_sent = 掃描倍率

掃描倍率    結論
─────────────────────────────
< 10        正常
10 ~ 100    可接受，但可優化
> 100       效率差，建議加 Index
> 1000      嚴重問題，必須處理
```

**範例：**
- `rows_examined: 50000, rows_sent: 1` → 掃描倍率 = 50000，嚴重問題
- `rows_examined: 100, rows_sent: 50` → 掃描倍率 = 2，正常

---

## 常見情況速查表

| 情況 | 特徵 | 結論 |
|------|------|------|
| 明確需要 Index | `no_index_count = total_exec` 且 `rows_examined >> rows_sent` | 需要加 Index |
| 小表正常 | `rows_examined` < 1000 且 `rows_examined ≈ rows_sent` | Full Scan 是正常的 |
| 部分沒用 Index | `no_index_count < total_exec` | 可能是參數不同導致，用 `--verbose` 檢查 |
| Read 打到 Primary | Role = `Primary` 但是 SELECT query | 確認 AP 連線設定 |
| SQL 找不到 | 輸出顯示 "not found on any pod" | SQL 已從 buffer 淘汰，重新觸發後再查 |

---

## FAQ

### Q: `no_index_count` 和 `total_exec` 相等代表什麼？

**A:** 代表這個 SQL 模板（pattern）每次執行都沒有用到 Index。這通常表示需要加 Index，但也可能是小表（< 1000 rows）Full Scan 是正常的。

### Q: `rows_examined` 很大但 `rows_sent` 很小是什麼意思？

**A:** 代表資料庫掃描了很多資料，但只回傳少量結果。這是典型的「需要加 Index」的情況。例如 `rows_examined: 50000, rows_sent: 1` 表示掃了 5 萬行只找到 1 行。

### Q: 為什麼有些 pod 顯示 `[Not found in history]`？

**A:** 這是正常的。不是每個 pod 都會執行同一個 SQL。常見情況：
- Write query 只會在 Primary 執行
- Read query 可能只打到部分 Replica
- 負載平衡導致不同 pod 收到不同 query

### Q: 什麼時候該用 `--verbose`？

**A:** 當你需要知道：
- 實際掃描了多少行（`rows_examined`）
- SQL 的執行效率（`rows_examined` vs `rows_sent`）
- 每個 pod 的執行狀況

### Q: 什麼是 Digest？為什麼我看到的 SQL 參數都變成 `?`？

**A:** MariaDB 收到每條 SQL 後會做正規化（normalize）：移除參數值、統一大小寫、移除多餘空白，產生一個 **DIGEST_TEXT**（SQL 模板），再對它做 hash 產生 **DIGEST**（固定長度的指紋）。

```
原始 SQL:    SELECT * FROM orders WHERE id = 12345 AND status = 'active'
DIGEST_TEXT: SELECT * FROM `orders` WHERE `id` = ? AND `status` = ?
DIGEST:      abc123def456...（hash 值）
```

所有只差在參數值的 SQL 會得到同一個 DIGEST，這讓 MariaDB 可以聚合統計（同一類 SQL 執行了幾次、多慢）。

### Q: 我想看完整 SQL（含實際參數），要去哪裡找？

**A:** Performance Schema 有三層表，保留的內容不同：

| 表 | 有完整 SQL？ | 保留量 | 說明 |
|----|:---:|--------|------|
| `events_statements_history` | ✓ `SQL_TEXT` | 每 thread 最近 10 筆 | 最新的，最先被覆蓋 |
| `events_statements_history_long` | ✓ `SQL_TEXT` | 全域最近 10000 筆 | 多一些，但也會被淘汰 |
| `events_statements_summary_by_digest` | ✗ 只有 `DIGEST_TEXT` | 永久（直到重啟） | 只有模板（`?`），沒有實際參數 |

查詢方式：

```sql
-- 從 history 查完整 SQL（最近的）
SELECT SQL_TEXT, CURRENT_SCHEMA, ROWS_EXAMINED
FROM performance_schema.events_statements_history
WHERE DIGEST = '<your_digest>'
ORDER BY EVENT_ID DESC LIMIT 1;

-- 如果 history 沒有，查 history_long
SELECT SQL_TEXT, CURRENT_SCHEMA, ROWS_EXAMINED
FROM performance_schema.events_statements_history_long
WHERE DIGEST = '<your_digest>'
ORDER BY EVENT_ID DESC LIMIT 1;
```

### Q: 為什麼 history 裡找不到完整 SQL？

**A:** `history` 和 `history_long` 是滾動式的 buffer — 新的 SQL 會覆蓋舊的。這是 MariaDB 的設計，**不是系統問題，也不需要調整**。

完整 SQL 本來就不是設計來永久保存的。Digest 的用途是**聚合統計**（同一類 SQL 的效能趨勢），不是記錄每一次執行的參數。

**你能做的：**
- 拿到 `DIGEST_TEXT`（模板）就足以判斷效能問題 — 因為 `?` 的位置就是你的 WHERE 條件，加 index 看的是欄位不是參數值
- 如果真的需要看實際參數，讓 app 再跑一次同樣的操作，**然後立刻查 history**（趁還沒被覆蓋）
- App 端的 log 或 ORM debug mode 也能看到完整 SQL，不需要從 DB 端取

### Q: `DIGEST_TEXT` 中的 `?` 可以還原成實際值嗎？

**A:** 不行。正規化是單向的，`?` 無法還原。

但**大多數情況不需要實際參數值**：

| 你想做的事 | 需要實際參數嗎 | 用什麼 |
|-----------|:---:|--------|
| 判斷要不要加 index | ✗ | `DIGEST_TEXT` — 看 WHERE 的欄位就夠了 |
| 分析 query 效能 | ✗ | `summary_by_digest` — 有執行次數、掃描行數、耗時 |
| 用 EXPLAIN 分析 | △ | 用任意參數值代入 `?` 即可，結果一樣 |
| Debug 特定資料問題 | ✓ | 從 app log 或 ORM debug mode 取得 |
