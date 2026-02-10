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
