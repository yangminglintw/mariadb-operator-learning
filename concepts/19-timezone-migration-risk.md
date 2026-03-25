# 生產環境 MariaDB 時區遷移風險指南（UTC+8 → UTC+0）

從影響評估到溝通清單，完整理解時區變更的風險與因應策略。

---

## 一、為什麼要改時區？

常見的遷移動機：

| 動機 | 說明 |
|------|------|
| 國際化 | 多時區團隊協作，統一 UTC 避免混亂 |
| Cloud Native 標準 | Kubernetes / logging / observability 生態統一用 UTC |
| 避免夏令時問題 | UTC 不受 DST 影響，排程穩定 |
| 簡化跨系統串接 | 與 Kafka、Elasticsearch、Prometheus 等系統時間對齊 |

**類比**：改時區就像把辦公室的掛鐘從「台灣時間」調成「倫敦時間」。
- 事件（會議、打卡）本身沒變
- 但看鐘的人如果不知道鐘改了，就會「遲到 8 小時」
- **DATETIME 就是那個只看掛鐘的人** — 它不知道鐘改了

---

## 二、DATETIME vs TIMESTAMP 核心差異

這是理解時區遷移風險的**最關鍵知識**。

### 對比表

| 特性 | DATETIME | TIMESTAMP |
|------|----------|-----------|
| 儲存方式 | **原樣儲存**（不做時區轉換） | **轉成 UTC** 儲存 |
| 顯示方式 | **原樣顯示** | 依 session `time_zone` 轉換後顯示 |
| 範圍 | 1000-01-01 ~ 9999-12-31 | 1970-01-01 ~ 2038-01-19 |
| 大小 | 8 bytes | 4 bytes |
| 時區變更後 | **值不變，但「語義」偏移** | **值正確，顯示隨時區改變** |

### 寫入/讀取流程圖

```
╔═══════════════════════════════════════════════════════════════╗
║  DATETIME 寫入/讀取                                           ║
╠═══════════════════════════════════════════════════════════════╣
║                                                               ║
║  App 寫入: INSERT ... VALUES('2026-03-25 14:00:00')           ║
║      │                                                        ║
║      ▼                                                        ║
║  磁碟儲存: 2026-03-25 14:00:00  ← 原樣存，不管 time_zone     ║
║      │                                                        ║
║      │  time_zone 從 '+08:00' 改成 '+00:00'                  ║
║      ▼                                                        ║
║  讀取顯示: 2026-03-25 14:00:00  ← 值一模一樣                 ║
║                                                               ║
║  問題：之前代表「台灣下午 2 點」                               ║
║        現在被誤解為「UTC 下午 2 點」= 台灣晚上 10 點          ║
║        ➜ 語義偏移 8 小時！                                    ║
╚═══════════════════════════════════════════════════════════════╝

╔═══════════════════════════════════════════════════════════════╗
║  TIMESTAMP 寫入/讀取                                          ║
╠═══════════════════════════════════════════════════════════════╣
║                                                               ║
║  App 寫入: INSERT ... VALUES('2026-03-25 14:00:00')           ║
║      │  time_zone='+08:00'，減 8 小時轉 UTC                  ║
║      ▼                                                        ║
║  磁碟儲存: 2026-03-25 06:00:00 (UTC)  ← 儲存的是 UTC         ║
║      │                                                        ║
║      │  time_zone 從 '+08:00' 改成 '+00:00'                  ║
║      ▼                                                        ║
║  讀取顯示: 2026-03-25 06:00:00  ← UTC 時間，值正確           ║
║                                                               ║
║  變化：之前顯示 14:00（+08:00 呈現）                          ║
║        現在顯示 06:00（+00:00 呈現）                          ║
║        ➜ 同一個時刻，只是顯示方式不同（都是正確的）           ║
╚═══════════════════════════════════════════════════════════════╝
```

### SQL 驗證範例

在你的 MariaDB 上親自驗證：

```sql
-- 建立測試表
CREATE TABLE IF NOT EXISTS tz_test (
  id INT AUTO_INCREMENT PRIMARY KEY,
  dt DATETIME,
  ts TIMESTAMP
);

-- 設定 session 為 +08:00，插入資料
SET time_zone = '+08:00';
INSERT INTO tz_test (dt, ts) VALUES (NOW(), NOW());

-- 查看原始值
SELECT '--- time_zone = +08:00 ---' AS info;
SELECT id, dt, ts, NOW() AS current_now FROM tz_test;

-- 切到 +00:00，再看一次
SET time_zone = '+00:00';
SELECT '--- time_zone = +00:00 ---' AS info;
SELECT id, dt, ts, NOW() AS current_now FROM tz_test;

-- 觀察重點：
-- dt 欄位：值完全不變（但語義已偏移 8 小時）
-- ts 欄位：值改變了（減 8 小時），但代表的時刻相同
-- NOW()：返回值提前 8 小時

-- 清理
DROP TABLE IF EXISTS tz_test;
```

**預期輸出**：

```
--- time_zone = +08:00 ---
+----+---------------------+---------------------+---------------------+
| id | dt                  | ts                  | current_now         |
+----+---------------------+---------------------+---------------------+
|  1 | 2026-03-25 11:00:00 | 2026-03-25 11:00:00 | 2026-03-25 11:00:00 |
+----+---------------------+---------------------+---------------------+

--- time_zone = +00:00 ---
+----+---------------------+---------------------+---------------------+
| id | dt                  | ts                  | current_now         |
+----+---------------------+---------------------+---------------------+
|  1 | 2026-03-25 11:00:00 | 2026-03-25 03:00:00 | 2026-03-25 03:00:00 |
+----+---------------------+---------------------+---------------------+
         ↑ 沒變！危險！          ↑ 減了 8 小時，正確       ↑ 也減了 8 小時
```

---

## 三、受影響範圍全景圖

| 項目 | 風險等級 | 影響說明 |
|------|----------|----------|
| 既有 **DATETIME** 欄位 | **高（危險）** | 值不變但語義偏移 8 小時，查詢結果會「錯」 |
| 既有 **TIMESTAMP** 欄位 | **中（注意）** | 值正確，但 App 如果直接讀字串（不處理 TZ），顯示會偏移 |
| `NOW()` / `CURRENT_TIMESTAMP` | **高** | 返回值立即提前 8 小時 |
| `CURDATE()` | **高** | 在 UTC 00:00~07:59 之間，返回的日期會「少一天」 |
| `CURTIME()` | **高** | 返回值提前 8 小時 |
| `created_at` / `updated_at` | **依欄位型別** | DATETIME → 高風險，TIMESTAMP → 低風險 |
| `WHERE created_at > '2026-03-25'` | **高** | 如果 created_at 是 DATETIME，查詢邊界偏移 |
| `GROUP BY DATE(created_at)` | **高** | 分組邊界偏移，報表數字會「不對」 |
| Event Scheduler | **高** | 排程觸發時間提前 8 小時 |
| Cron / 排程任務（App 層） | **高** | 如果用 DB 時間比較，邏輯會錯 |
| Slow query log 時戳 | **低** | 僅影響 log 分析，不影響業務 |
| Error log / General log | **低** | 時戳格式改變 |

### 最危險的情境

```
場景：電商系統，orders 表有 DATETIME 型別的 order_date

改 TZ 之前（+08:00）：
  SELECT COUNT(*) FROM orders WHERE order_date >= '2026-03-25' AND order_date < '2026-03-26';
  → 返回 2026-03-25 台灣時間的訂單數量 ✓

改 TZ 之後（+00:00）：
  同一個查詢，同一個資料 → 但「2026-03-25」現在代表 UTC
  → 實際上查到的是台灣時間 03-25 08:00 ~ 03-26 08:00 的訂單
  → 結果偏移 8 小時！報表數字不一致！
```

---

## 四、Replication 期間的特殊風險

使用 mariadb-operator 時，修改 MariaDB CR 會觸發 **Rolling Update**（逐一重啟 pod）。
這意味著在重啟過程中，會有一段**混合時區窗口**。

### Rolling Update 時序圖

```
時間軸 ──────────────────────────────────────────────────────►

Pod-0 (Primary)  [+08:00] ───── restart ───── [+00:00] ─────
Pod-1 (Replica)  [+08:00] ──────────── restart ───── [+00:00]
Pod-2 (Replica)  [+08:00] ─────────────────── restart ── [+00:00]

                           ◄── 混合時區窗口 ──►
                           Primary 已是 +00:00
                           Replica 還是 +08:00
```

### Statement-Based vs Row-Based Replication

| 項目 | Statement-Based (SBR) | Row-Based (RBR) |
|------|----------------------|-----------------|
| TIMESTAMP 欄位 | 安全 — binlog 帶 `SET TIMESTAMP=N` | 安全 — 儲存的是 UTC |
| DATETIME + `NOW()` | **危險** — replica 的 `NOW()` 可能不同 | 安全 — binlog 記的是實際值 |
| DATETIME + 字串值 | 安全 — 原樣複製 | 安全 — 原樣複製 |

**重要**：確認 `binlog_format=ROW`。ROW 格式在時區遷移中更安全，因為 binlog 記錄的是**實際資料值**，不是 SQL 語句。

```sql
-- 確認 binlog format
SELECT @@global.binlog_format;
-- 預期：ROW
```

### 混合時區窗口的風險

在 rolling update 期間，如果 App 讀取到不同 replica：
- 讀 Pod-1（已更新，+00:00）→ `NOW()` 返回 UTC
- 讀 Pod-2（未更新，+08:00）→ `NOW()` 返回 台灣時間
- 如果 App 用 `NOW()` 做業務邏輯 → **同一秒的請求可能得到不同結果**

**建議**：在低流量時段執行，或短暫暫停依賴 `NOW()` 的業務流程。

---

## 五、實用診斷查詢

在遷移前執行以下查詢，**盤點影響範圍**。

### 5.1 盤點所有 DATETIME 欄位

```sql
SELECT
  TABLE_SCHEMA,
  TABLE_NAME,
  COLUMN_NAME,
  COLUMN_TYPE,
  IS_NULLABLE,
  COLUMN_DEFAULT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE DATA_TYPE = 'datetime'
  AND TABLE_SCHEMA NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION;
```

### 5.2 盤點所有 TIMESTAMP 欄位

```sql
SELECT
  TABLE_SCHEMA,
  TABLE_NAME,
  COLUMN_NAME,
  COLUMN_TYPE,
  IS_NULLABLE,
  COLUMN_DEFAULT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE DATA_TYPE = 'timestamp'
  AND TABLE_SCHEMA NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION;
```

### 5.3 使用 NOW()/CURRENT_TIMESTAMP 作為 DEFAULT 的欄位

這些欄位在時區變更後，新插入的資料會立即受影響。

```sql
SELECT
  TABLE_SCHEMA,
  TABLE_NAME,
  COLUMN_NAME,
  DATA_TYPE,
  COLUMN_DEFAULT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE (COLUMN_DEFAULT LIKE '%current_timestamp%'
       OR COLUMN_DEFAULT LIKE '%NOW()%')
  AND TABLE_SCHEMA NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
ORDER BY TABLE_SCHEMA, TABLE_NAME;
```

### 5.4 Event Scheduler 排程

```sql
SELECT
  EVENT_SCHEMA,
  EVENT_NAME,
  EXECUTE_AT,
  INTERVAL_VALUE,
  INTERVAL_FIELD,
  LAST_EXECUTED,
  STATUS
FROM INFORMATION_SCHEMA.EVENTS
WHERE STATUS = 'ENABLED';
```

### 5.5 確認當前設定

```sql
SELECT
  @@global.time_zone AS global_tz,
  @@session.time_zone AS session_tz,
  NOW() AS now_value,
  UTC_TIMESTAMP() AS utc_value,
  TIMEDIFF(NOW(), UTC_TIMESTAMP()) AS tz_offset,
  @@global.binlog_format AS binlog_format;
```

### 5.6 統計各資料庫的 DATETIME vs TIMESTAMP 欄位數量

快速了解影響規模：

```sql
SELECT
  TABLE_SCHEMA,
  SUM(CASE WHEN DATA_TYPE = 'datetime' THEN 1 ELSE 0 END) AS datetime_count,
  SUM(CASE WHEN DATA_TYPE = 'timestamp' THEN 1 ELSE 0 END) AS timestamp_count
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
GROUP BY TABLE_SCHEMA
ORDER BY datetime_count DESC;
```

---

## 六、mariadb-operator 中的時區設定方式

### 方式 1：MariaDB CR 的 myCnf（推薦）

```yaml
apiVersion: k8s.mariadb.com/v1alpha1
kind: MariaDB
metadata:
  name: mariadb-production
spec:
  myCnf: |
    [mariadb]
    default_time_zone='+00:00'
```

修改 CR 後：
1. Operator 更新 ConfigMap（my.cnf）
2. 觸發 StatefulSet Rolling Update
3. Pod 逐一重啟，套用新設定
4. **Rolling Update 期間有混合時區窗口**（見第四章）

### 方式 2：init_connect（Session 層級）

```yaml
spec:
  myCnf: |
    [mariadb]
    init_connect='SET time_zone="+00:00"'
```

**限制**：
- `init_connect` 對 **SUPER 權限的使用者不生效**（包括 root）
- Operator 內部連線通常使用 SUPER 權限 → operator 的操作仍然用 SYSTEM timezone
- 適合作為**過渡方案**，不適合作為最終方案

### 方式 3：App 連線時設定（最安全但最麻煩）

每個 App 在建立連線後執行：

```sql
SET time_zone = '+00:00';
```

或在連線字串中指定（依 driver 而定）。

**優點**：不需要重啟 DB，不影響其他使用者
**缺點**：需要所有 App 都修改，容易遺漏

### 三種方式比較

| | myCnf（全域） | init_connect | App 連線設定 |
|--|---------------|-------------|-------------|
| 影響範圍 | 所有連線 | 非 SUPER 連線 | 單一 App |
| 需要重啟 | 是（Rolling Update） | 是 | 否 |
| 混合時區窗口 | 有 | 有 | 無 |
| SUPER 使用者 | 受影響 | **不受影響** | 受影響（如果有設定） |
| 遺漏風險 | 無 | SUPER 使用者 | 高（App 很多時容易漏） |

---

## 七、遷移策略與步驟

### Phase 1：評估（建議 1-2 週）

```
┌─────────────────────────────────────────────────────────┐
│  1. 執行第五章的診斷查詢                                  │
│     └── 盤點所有 DATETIME / TIMESTAMP 欄位               │
│                                                          │
│  2. 與應用團隊確認每個 DATETIME 欄位的語義               │
│     ├── 存的是 local time（台灣時間）？→ 高風險          │
│     ├── 存的是 UTC？→ 低風險（已經是 UTC）               │
│     └── 存的是 App 自己產生的時間？→ 中風險              │
│                                                          │
│  3. 確認 binlog_format = ROW                             │
│                                                          │
│  4. 建立影響矩陣（每個 App × 每個表 × 風險等級）        │
└─────────────────────────────────────────────────────────┘
```

### Phase 2：準備（建議 1 週）

- [ ] 在 staging/dev 環境完整驗證
- [ ] 確認 `binlog_format=ROW`
- [ ] 如需調整既有 DATETIME 資料，準備 migration script：

```sql
-- 範例：將 DATETIME 欄位的值減 8 小時（從 local time 轉為 UTC）
-- ⚠️ 這是不可逆操作，務必先備份！
-- UPDATE orders SET order_date = order_date - INTERVAL 8 HOUR;
```

- [ ] 準備 rollback 計畫
- [ ] 通知所有相關團隊（使用第八章的溝通清單）
- [ ] 選定執行時段（低流量時段）

### Phase 3：執行

```
執行順序：

1. 通知所有團隊：「即將開始時區遷移」
      │
2. 備份資料庫（或確認 point-in-time recovery 可用）
      │
3. [選擇性] 暫停依賴 NOW() 的排程任務
      │
4. 修改 MariaDB CR：
   kubectl --context=<CTX> -n <NS> edit mariadb <NAME>
   # 加入 default_time_zone='+00:00'
      │
5. 監控 Rolling Update 進度：
   kubectl --context=<CTX> -n <NS> rollout status sts <STS>
      │
6. [選擇性] 執行 DATETIME 資料調整 migration
      │
7. 恢復排程任務
      │
8. 通知所有團隊：「遷移完成，請驗證」
```

### Phase 4：驗證

```sql
-- 1. 確認 time_zone 已更改
SELECT @@global.time_zone, NOW(), UTC_TIMESTAMP();
-- 預期：NOW() ≈ UTC_TIMESTAMP()

-- 2. 確認 TIMESTAMP 欄位讀取正確
-- （比較已知時間點的資料）

-- 3. 確認 replication 正常
SHOW SLAVE STATUS\G
-- 檢查 Seconds_Behind_Master、Last_IO_Error、Last_SQL_Error
```

- [ ] 監控 24 小時
- [ ] 確認報表數字正確
- [ ] 確認 Event Scheduler 在正確時間觸發
- [ ] 確認 App 功能正常

### Rollback Plan

如果需要回滾：

```yaml
# 改回 +08:00
spec:
  myCnf: |
    [mariadb]
    default_time_zone='+08:00'
```

**⚠️ Rollback 注意**：如果在 UTC 模式下已有新資料用 `NOW()` 寫入 DATETIME 欄位，這些資料在回滾後會偏差 8 小時（本來是 UTC 的值，現在被當成 +08:00 解讀）。

---

## 八、應用團隊溝通清單

以下清單可直接複製發送給應用團隊。

---

### 時區變更通知

**變更內容**：MariaDB 全域 `time_zone` 從 `+08:00` 改為 `+00:00`（UTC）

**變更時間**：____（填入計畫時間）

**影響摘要**：
- `NOW()` 返回值將**提前 8 小時**（從台灣時間變成 UTC）
- TIMESTAMP 欄位：儲存值不變，但顯示值提前 8 小時
- DATETIME 欄位：儲存值和顯示值都不變，但**語義偏移 8 小時**

### 請各團隊確認以下事項

- [ ] 你的應用是否使用 `NOW()` / `CURRENT_TIMESTAMP` 做業務邏輯？
  - 例如：`WHERE created_at > NOW() - INTERVAL 1 HOUR`
  - 影響：條件會偏移 8 小時
- [ ] 你的應用是否有 **DATETIME** 欄位存放本地時間（台灣時間）？
  - 影響：既有資料語義偏移，新資料如果用 `NOW()` 寫入會是 UTC
- [ ] 你的應用是否有排程任務依賴 DB 時間？
  - 例如：每天凌晨 2 點執行的 Job 用 DB 的 `NOW()` 判斷
  - 影響：判斷邏輯偏移 8 小時
- [ ] 你的應用是否有跨時區的日期比較邏輯？
- [ ] 你的報表查詢是否用日期做 `GROUP BY`？
  - 影響：分組邊界偏移，可能導致數字不一致
- [ ] 你的應用連線時是否已設定 `session time_zone`？
  - 如果有 → 影響較小（由 session 設定決定）
  - 如果沒有 → 受全域設定影響

### 建議行動

| 你的情況 | 建議 |
|---------|------|
| 只有 TIMESTAMP 欄位 | 確認 App/前端有正確處理時區轉換即可 |
| 有 DATETIME 欄位存 local time | **決定是否批量調整既有資料**（`- INTERVAL 8 HOUR`） |
| 使用 `NOW()` 做業務邏輯 | 改用 `UTC_TIMESTAMP()` 或在 App 層產生時間 |
| 報表用日期 GROUP BY | 調整查詢的日期範圍條件 |
| 排程任務依賴 DB 時間 | 調整觸發條件 |
| App 連線未設 session time_zone | 評估是否在連線時加上 `SET time_zone='+00:00'` |

### 常見問題

**Q: 改完之後，舊資料會不會消失或被改掉？**
A: 不會。儲存在磁碟上的資料**完全不會變動**。風險在於「解讀方式」改變。

**Q: TIMESTAMP 欄位安全嗎？**
A: 儲存層面完全安全（因為本來就存 UTC）。但如果 App 直接拿 `SELECT ts FROM table` 的字串結果顯示給使用者，會看到 UTC 時間而非台灣時間。

**Q: 可以只改某些資料庫嗎？**
A: `time_zone` 是 global/session 層級的設定，**不是 database 層級**。改了就全改。但可以讓個別 App 在連線時設定自己的 session time_zone。

**Q: 改完之後 `CURDATE()` 會怎樣？**
A: 在台灣時間 00:00~07:59 之間（即 UTC 16:00~23:59 前一天），`CURDATE()` 會返回前一天的日期。這對每日結算、日報等邏輯影響重大。
