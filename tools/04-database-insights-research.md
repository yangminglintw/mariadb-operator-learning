# AWS CloudWatch Database Insights 研究報告

## 1. 概述

AWS CloudWatch Database Insights 是 AWS 在 2024 年 12 月發布的資料庫監控解決方案，用於取代即將在 **2026 年 6 月 30 日**棄用的 RDS Performance Insights。

### 支援的資料庫引擎

- Amazon Aurora (PostgreSQL / MySQL)
- Amazon RDS for PostgreSQL
- Amazon RDS for MySQL
- **Amazon RDS for MariaDB**
- Amazon RDS for SQL Server
- Amazon RDS for Oracle

### 官方文件連結

- [CloudWatch Database Insights 文件](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Database-Insights.html)
- [Database Insights 實際應用案例](https://aws.amazon.com/blogs/database/amazon-cloudwatch-database-insights-applied-in-real-scenarios/)
- [從 Performance Insights 遷移到 Database Insights](https://repost.aws/articles/AR6gPnT__dQdq81Md6Q_A1mA/transitioning-from-rds-performance-insights-to-cloudwatch-database-insights)

---

## 2. 核心功能分析

### 2.1 兩種運作模式

| 模式 | 資料保留 | 功能範圍 | 費用 |
|------|----------|----------|------|
| **Standard** | 7 天 | 基本指標、等待事件 | 免費 |
| **Advanced** | 15 個月 | 完整功能（見下方） | 付費 |

### 2.2 Advanced Mode 功能

1. **Top SQL 分析**
   - 顯示對資料庫負載最重的 SQL 語句
   - 提供 29+ 個額外指標
   - 可追蹤 SQL 執行統計

2. **等待事件（Wait Events）**
   - 分析效能瓶頸來源
   - CPU / IO / Lock 等待分類
   - 與 SQL 關聯分析

3. **執行計畫分析**
   - SQL 執行計畫捕獲（RDS MySQL/MariaDB 需 2025/02 後支援）
   - Lock 分析

4. **異常偵測（Anomaly Detection）**
   - 使用機器學習模型識別效能瓶頸
   - 自動比較選定時段與正常基線
   - 提供具體修復建議
   - 將診斷時間從數小時縮短至數分鐘

5. **On-Demand 分析**
   - 支援 RDS for MySQL 和 MariaDB（2025/02 起）
   - 歷史資料分析
   - [公告連結](https://aws.amazon.com/about-aws/whats-new/2025/02/database-insights-on-demand-analysis-rds-mysql-rds-mariadb/)

6. **Fleet 監控**
   - 跨多個資料庫實例的統一儀表板
   - 集中式告警管理

---

## 3. 功能比較表

| 功能 | AWS Database Insights | Percona PMM | Prometheus + Grafana |
|------|----------------------|-------------|---------------------|
| **SQL Digest 收集** | ✅ Advanced | ✅ QAN | ⚠️ 需 sql_exporter |
| **完整 SQL 查詢** | ✅ Advanced | ✅ QAN | ⚠️ 需額外設定 |
| **等待事件分析** | ✅ | ✅ | ⚠️ 部分支援 |
| **執行計畫捕獲** | ✅ Advanced (MySQL/MariaDB 2025/02+) | ✅ | ❌ |
| **異常偵測** | ✅ ML-based | ❌ | ❌ |
| **Top SQL 排名** | ✅ 29+ 指標 | ✅ | ⚠️ 需自建 |
| **Lock 分析** | ✅ Advanced | ✅ | ⚠️ 需自建 |
| **Fleet 監控** | ✅ | ✅ | ✅ |
| **自訂儀表板** | ⚠️ 有限 | ✅ Grafana | ✅ Grafana |
| **告警規則** | ✅ CloudWatch Alarms | ✅ 內建 Advisors | ✅ Alertmanager |
| **歷史資料保留** | 7 天 / 15 個月 | 自訂（無限制） | 自訂（無限制） |
| **支援 K8s MariaDB** | ❌ 僅 RDS/Aurora | ✅ | ✅ |
| **開源** | ❌ | ✅ | ✅ |

### 關鍵差異

| 面向 | AWS Database Insights | PMM | Prometheus Stack |
|------|----------------------|-----|------------------|
| **部署環境** | 僅 AWS RDS/Aurora | 任意環境 | 任意環境 |
| **設定複雜度** | 低（一鍵啟用） | 中 | 高 |
| **客製化彈性** | 低 | 高 | 最高 |
| **Vendor Lock-in** | 高 | 低 | 無 |

---

## 4. 成本分析

### 4.1 AWS CloudWatch Database Insights

**Standard Mode（免費）：**
- 7 天資料保留
- 基本指標和等待事件

**Advanced Mode：**
- Provisioned 實例：**$0.0125 / vCPU-hour**（約 $9 / vCPU-month）
- Aurora Serverless v2：**$0.003125 / ACU-hour**（約 $2.5 / ACU-month）
- 15 個月資料保留

**範例計算（3 節點 MariaDB，每節點 4 vCPU）：**
```
月費 = 3 節點 × 4 vCPU × $9 = $108 / month
年費 = $108 × 12 = $1,296 / year
```

**與舊版 Performance Insights 比較：**
- Database Insights Advanced 約為 Performance Insights 付費版的 **6 倍價格**
- 資料保留從 24 個月縮短為 15 個月

參考：[RDS Performance Insights 定價](https://aws.amazon.com/rds/performance-insights/pricing/)

### 4.2 Percona PMM

**軟體費用：免費（開源）**

**基礎設施成本（自架）：**

| 規模 | PMM Server 規格 | 估算月費 |
|------|----------------|----------|
| 小型（< 10 DB） | 2 vCPU / 4GB RAM / 100GB SSD | ~$50-80 |
| 中型（10-50 DB） | 4 vCPU / 16GB RAM / 500GB SSD | ~$150-250 |
| 大型（50+ DB） | 8+ vCPU / 32GB+ RAM / 1TB+ SSD | ~$400+ |

**優點：**
- PMM 3.5.0+ 所有 Advisors 和告警模板完全免費
- 無訂閱費用
- 資料保留無限制

參考：[PMM 官方文件](https://docs.percona.com/percona-monitoring-and-management/3/index.html)

### 4.3 Prometheus + Grafana

**軟體費用：免費（開源）**

**基礎設施成本：**

| 元件 | 規格建議 | 月費估算 |
|------|----------|----------|
| Prometheus Server | 2-4 vCPU / 8-16GB RAM | ~$80-150 |
| Grafana | 1-2 vCPU / 2-4GB RAM | ~$30-50 |
| 儲存（長期） | Thanos/Cortex + S3 | ~$20-100 |

**總計：**~$130-300 / month（中型規模）

### 4.4 成本比較總結

| 方案 | 小型環境月費 | 中型環境月費 | 大型環境月費 |
|------|-------------|-------------|-------------|
| AWS Database Insights | ~$50-100 | ~$200-400 | ~$500-1000+ |
| Percona PMM | ~$50-80 | ~$150-250 | ~$400+ |
| Prometheus Stack | ~$80-150 | ~$130-300 | ~$300-500+ |

> **注意：** AWS 方案僅適用於 RDS/Aurora，不適用於 K8s 自建 MariaDB

---

## 5. 實作難度分析

### 5.1 AWS Database Insights

**優點：**
- 一鍵啟用，無需管理額外基礎設施
- 與 CloudWatch 生態系整合
- AWS 負責維護和升級

**缺點：**
- 僅限 RDS/Aurora
- 客製化彈性低
- Vendor lock-in

**適合：**
- 使用 RDS/Aurora 的團隊
- DevOps 資源有限的團隊
- 需要快速上手的場景

### 5.2 Percona PMM

**優點：**
- 開箱即用的 Query Analytics
- 豐富的內建儀表板和 Advisors
- 支援多種資料庫（MySQL、PostgreSQL、MongoDB）
- 可監控 RDS 和自建資料庫

**缺點：**
- 需要管理 PMM Server
- 學習曲線中等

**適合：**
- 混合環境（RDS + K8s）
- 需要深入 Query 分析的團隊
- 已有 Percona 生態系經驗的團隊

### 5.3 Prometheus + Grafana

**優點：**
- 最高度客製化
- 與 K8s 生態系完美整合
- 社群資源豐富

**缺點：**
- Query Analytics 需自行實作
- 需要較多 DevOps 投入
- 元件整合需要經驗

**適合：**
- K8s 原生環境
- 已有 Prometheus 監控棧的團隊
- 需要高度客製化的場景

---

## 6. 適用場景建議

### 6.1 選擇 AWS Database Insights 的情境

✅ **推薦使用：**
- 100% 使用 AWS RDS/Aurora
- 團隊 DevOps 資源有限
- 需要 ML-based 異常偵測
- 可以接受較高成本換取便利

❌ **不適合：**
- 使用 K8s 自建 MariaDB
- 需要長期資料保留（> 15 個月）
- 預算敏感

### 6.2 選擇 Percona PMM 的情境

✅ **推薦使用：**
- 混合環境（RDS + K8s 自建）
- 需要深入的 Query Analytics
- 預算有限但需要專業級功能
- 需要監控多種資料庫類型

❌ **不適合：**
- 不想管理額外基礎設施
- 團隊無 Linux/Docker 管理經驗

### 6.3 選擇 Prometheus + Grafana 的情境

✅ **推薦使用：**
- 已有成熟的 Prometheus 監控棧
- K8s 原生環境
- 需要高度客製化
- 技術能力強的團隊

❌ **不適合：**
- 需要開箱即用的 Query Analytics
- DevOps 資源有限
- 需要快速上線

### 6.4 決策流程圖

```
                    ┌─────────────────────┐
                    │ 資料庫部署在哪裡？  │
                    └─────────┬───────────┘
                              │
              ┌───────────────┴───────────────┐
              │                               │
              ▼                               ▼
       ┌────────────┐                  ┌────────────┐
       │ AWS RDS/   │                  │ K8s 自建   │
       │ Aurora     │                  │ MariaDB    │
       └─────┬──────┘                  └─────┬──────┘
             │                               │
             ▼                               ▼
    ┌────────────────┐            ┌──────────────────┐
    │ 需要深入 Query │            │ 已有 Prometheus? │
    │ Analytics?     │            └────────┬─────────┘
    └───────┬────────┘                     │
            │                    ┌─────────┴─────────┐
   ┌────────┴────────┐           │                   │
   │                 │           ▼                   ▼
   ▼                 ▼      ┌─────────┐        ┌─────────┐
┌──────┐        ┌───────┐   │  是     │        │  否     │
│ 否   │        │ 是    │   └────┬────┘        └────┬────┘
└──┬───┘        └───┬───┘        │                  │
   │                │            ▼                  ▼
   ▼                ▼       ┌─────────┐       ┌──────────┐
┌──────────┐   ┌─────────┐  │Prometheus│      │ PMM 或   │
│ Database │   │ PMM     │  │+ Grafana │      │Prometheus│
│ Insights │   │         │  └──────────┘      └──────────┘
│ Standard │   └─────────┘
└──────────┘
```

---

## 7. 結論

### 對於本專案（K8s MariaDB Operator）的建議

由於我們使用的是 **Kubernetes 自建 MariaDB**（透過 mariadb-operator），AWS Database Insights **不適用**。建議方案：

1. **首選：Prometheus + Grafana + PMM**
   - 使用 Prometheus 收集基礎指標（mysqld_exporter）
   - 使用 PMM 進行深入的 Query Analytics
   - 兩者可以並存

2. **次選：純 Prometheus Stack**
   - 搭配 sql_exporter 實作 SQL Digest 收集
   - 需要更多自訂開發

3. **如果未來遷移到 RDS：**
   - 可考慮 Database Insights Advanced
   - 或繼續使用 PMM（同時監控 RDS）

### 重點摘要

| 指標 | AWS Database Insights | 開源方案（PMM/Prometheus）|
|------|----------------------|---------------------------|
| 適用範圍 | 僅 RDS/Aurora | 任意環境 |
| 成本 | 較高 | 較低 |
| 維運負擔 | 低 | 中-高 |
| 客製化 | 低 | 高 |
| Query Analytics | 優秀 | 優秀（PMM）/ 需自建（Prometheus）|

---

## 參考資料

1. [CloudWatch Database Insights 官方文件](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Database-Insights.html)
2. [Performance Insights 到 Database Insights 遷移指南](https://repost.aws/articles/AR6gPnT__dQdq81Md6Q_A1mA/transitioning-from-rds-performance-insights-to-cloudwatch-database-insights)
3. [AWS Performance Insights 棄用說明](https://pganalyze.com/blog/aws-performance-insights-deprecation-database-insights-comparison)
4. [Percona PMM 官方文件](https://docs.percona.com/percona-monitoring-and-management/3/index.html)
5. [PMM Query Analytics](https://docs.percona.com/percona-monitoring-and-management/2/get-started/query-analytics.html)
6. [Prometheus mysqld_exporter](https://github.com/prometheus/mysqld_exporter)
7. [RDS Performance Insights 定價](https://aws.amazon.com/rds/performance-insights/pricing/)
