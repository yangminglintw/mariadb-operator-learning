# MariaDB CR & Replication 完整 Issues 清單

**日期**: 2026-01-22
**目的**: 方便下次挑選要做的 issue

---

## 📋 所有 Replication / MariaDB CR 相關 Issues

### 🔴 Multi-cluster Topology (Roadmap 重點)

| # | Title | Labels | 說明 | 適合度 |
|---|-------|--------|------|--------|
| **1543** | Multi-cluster topology | feature, multi-cluster | 跨 K8s 叢集 replication | ❌ 太複雜 - Roadmap 優先 |
| **1214** | Make server_id configurable | feature, replication, multi-cluster | server_id 目前是 10+podIndex | ✅ **推薦！** |
| **1218** | Primary replicating from external | feature, multi-cluster | Geo-replication 需求 | ❌ 太複雜 |
| **1177** | Support replication from external | feature, replication | 從外部 MariaDB 複製 (PR #1437 進行中) | ❌ 已有 PR |
| **76** | Two way replication / circular async | feature, replication, multi-cluster | 雙向複製 | ❌ 太複雜 |

### 🟡 Replication Bugs

| # | Title | Labels | 說明 | 適合度 |
|---|-------|--------|------|--------|
| **1499** | Stuck on "Switching primary to" | bug, replication, stale | Primary switchover 卡住 | ⚠️ 需要 replication 環境 |
| **1235** | Slave I/O error 1236 (GTID) | feature | GTID 複製錯誤 | ❌ 不是 bug，是操作問題 |
| **1508** | Replica recovery doesn't work | bug, stale | Replica 恢復失敗 (sql_log_bin) | ✅ 簡單 fix |
| **1509** | Failover clients stuck read-only | bug, stale | Relevant to my use case | ⚠️ 架構性問題 |

### 🟢 Galera Issues

| # | Title | Labels | 說明 | 適合度 |
|---|-------|--------|------|--------|
| **929** | Async replication stopped with Galera | bug, galera, replication | Galera + async replication 衝突 | ⚠️ 需要 Galera 環境 |
| **1477** | Galera unable to recover from shutdown | bug | Galera 恢復問題 | ⚠️ 需要 Galera 環境 |
| **1503** | Operator upgrade failures with Galera | bug | 升級問題 | ⚠️ 需要 Galera 環境 |
| **1447** | volumeClaimTemplate.labels not mutable | bug | Galera PVC label 問題 | ⚠️ 中等 |
| **906** | inheritMetadata for Galera PVC | good first issue, feature | Galera PVC 的 inheritMetadata | ✅ 簡單！ |
| **785** | Remove GaleraAgent sidecar | feature, galera | 移除 sidecar | ⚠️ 架構性 |
| **591** | Join existing Galera cluster | feature, galera, multi-cluster | 跨 cluster Galera | ❌ 太複雜 |
| **220** | Galera cross-cluster support | feature, galera, multi-cluster | 跨 cluster 支援 | ❌ 太複雜 |
| **219** | Galera recovery documentation | feature, galera | 文件請求 | ✅ 文件 |
| **164** | Propagate log level to init/agent | feature, galera | Log level 傳遞 | ⚠️ 中等 |
| **85** | Replication together with Galera | feature, galera, multi-cluster | Galera + Replication | ❌ 太複雜 |
| **84** | Galera with Arbitrator | feature, galera | Galera 仲裁節點 | ⚠️ 中等 |

### 🔵 GTID / server_id Issues

| # | Title | Labels | 說明 | 適合度 |
|---|-------|--------|------|--------|
| **1214** | Make server_id configurable | feature, replication, multi-cluster | server_id base 可設定 | ✅ **推薦！** |
| **1530** | Unique gtid_domain_id per pod | bug, multi-cluster | 每個 Pod 需要唯一 GTID domain | ⚠️ 跟 #1504, #878 相關 |
| **1504** | How to set gtid_domain_id | bug, multi-cluster | 同上，重複 issue | ⚠️ 重複 |
| **878** | Can't configure galera.cnf for async | bug, multi-cluster | 設定檔問題 | ⚠️ 跟 #1530 相關 |

### 🟣 其他 MariaDB CR Features

| # | Title | Labels | 說明 | 適合度 |
|---|-------|--------|------|--------|
| **1292** | Fine-Grained Replica Control (KEDA) | feature | spec.manageReplicas for KEDA | ⚠️ 中等 |
| **1407** | MaxScale 24.x private_address | feature | MaxScale 新版支援 | ⚠️ 需要 MaxScale |
| **1486** | Cluster name label in metrics | feature | Metrics label | ✅ 簡單 |
| **356** | Scale down to 0 replicas | good first issue, feature | 允許縮減到 0 | ⚠️ 中等 |
| **1125** | CleanupPolicy for objects | good first issue, feature | 讓 operator 物件有 cleanup policy | ⚠️ 中等 |
| **1186** | Label for Pods created by operator | good first issue, feature | Pod label | ✅ 簡單 |

---

## 🎯 推薦的 Issues (依難度)

### ✅ Good First Issues (簡單)

| # | Title | 預估工作 |
|---|-------|---------|
| **906** | inheritMetadata for Galera PVC | CRD field 添加 |
| **1186** | Label for Pods | CRD field 添加 |
| **1508** | sql_log_bin fix | SQL 函數修改 |
| **1486** | Cluster name in metrics | Metrics label |
| **219** | Galera recovery docs | 文件 |

### ⚠️ 中等難度

| # | Title | 需要了解 |
|---|-------|---------|
| **1214** | server_id configurable | Replication init 流程 |
| **356** | Scale to 0 replicas | StatefulSet 行為 |
| **164** | Propagate log level | Container 設定 |
| **1125** | CleanupPolicy | K8s ownership |

### ❌ 高難度 / Maintainer 主導

| # | Title | 原因 |
|---|-------|------|
| **1543** | Multi-cluster topology | 核心架構變更 |
| **1177** | External replication | 已有 PR #1437 |
| **1218** | Primary from external | 架構變更 |
| **1509** | Kill connections | 需要討論設計 |

---

## 📊 統計

| 類別 | 數量 |
|------|------|
| Multi-cluster | 5 |
| Replication Bugs | 4 |
| Galera | 12 |
| GTID/server_id | 4 |
| 其他 Features | 6 |
| **總計** | 31 |

---

## 📈 關聯圖

```
#1543 Multi-cluster Topology (Roadmap)
├── #1214 server_id configurable ← 前置需求 ⭐
├── #1530 gtid_domain_id per pod
│   ├── #1504 (duplicate)
│   └── #878 galera.cnf editing
├── #1218 Primary from external
├── #1177 External replication (PR #1437)
└── ... more issues

Replication Bugs
├── #1499 Switchover stuck
├── #1508 Replica recovery (sql_log_bin) ← 簡單 fix
├── #1509 Failover kill connections ← Practical priority
└── #1235 GTID error (user operation)

Galera Issues
├── #929 Galera + async replication
├── #906 inheritMetadata ← Good first issue
└── ... more issues
```

---

## 🚀 下次挑選建議

1. **對齊 Roadmap**: 選 #1214 (server_id)
2. **簡單入手**: 選 #906, #1186, #1508
3. **Solve practical issues**: 選 #1509 (需要先跟 maintainer 討論)
4. **文件貢獻**: 選 #219

---

## 📝 備註

- 這份清單是 2026-01-22 的快照
- 新 issues 可能會出現
- 已標記 `stale` 的 issues 可能需要確認是否還有效
