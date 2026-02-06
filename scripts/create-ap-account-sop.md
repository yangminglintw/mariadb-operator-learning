# SOP: Create AP Run Account

## Step 0: Receive Support Ticket

收到建立 AP Run Account 的 support ticket，依目標環境分為兩種情況：

| Environment | PR 包含內容 |
|-------------|------------|
| **Test** | SQL + Account Name |
| **STG / PRD** | Site + Account Name |

## Step 1: Review PR

### Case A: 新帳號

- 直接 **approve** PR。
- 不需額外檢查 — Rundeck job 會自動驗證帳號。

### Case B: 既有帳號（修改權限）

- 確認使用者：**新的 grant SQL 是否已包含舊有的權限？**
- 每次執行會**完全覆蓋**帳號權限（非增量更新，無版本控制），因此新 SQL 必須包含所有需要的權限。
- 若新 SQL 未包含舊權限，請使用者更新 SQL，將新舊權限合併後再提交。

## Step 2: Execute Rundeck Jobs

PR approve 後，依序執行以下兩個 Rundeck jobs：

> **注意：** 目前沒有一步到位的 Rundeck job，必須分兩步驟執行。

### Step 2.1: Create Account & Send Mail

- **Rundeck Job URL:** `<TODO: fill in URL>`

此 job 會建立帳號並寄送通知信給使用者。建立完成後帳號僅有 **default auth**，尚未套用 PR 中的權限。

**參數填寫：**

| 參數欄位 | 填入值 | 來源 |
|---------|-------|------|
| `<TODO: param name>` | `<TODO: value>` | `<TODO: 從 PR / ticket 取得>` |
| `<TODO: param name>` | `<TODO: value>` | `<TODO: 從 PR / ticket 取得>` |

### Step 2.2: Regrant Account Auth

- **Rundeck Job URL:** `<TODO: fill in URL>`

因 Step 2.1 僅套用 default auth，需透過此步驟重新授權正確的權限。

**參數填寫：**

| 參數欄位 | 填入值 | 來源 |
|---------|-------|------|
| `<TODO: param name>` | `<TODO: value>` | `<TODO: 從 PR / Python script output>` |
| `<TODO: param name>` | `<TODO: value>` | `<TODO: 從 PR / Python script output>` |

**操作步驟：**
1. 從 PR 複製 SQL。
2. 將 SQL 貼入附錄的 Python script（取代 placeholder）。
3. 執行 Python script，取得輸出結果。
4. 將輸出結果貼到上方 Rundeck job 的對應參數欄位中執行。

## Appendix: Python Script for Regrant Auth

`<TODO: paste your Python script here>`
