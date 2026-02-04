# Kubernetes Operator 模式

## 什麼是 Operator？

Operator 是一種 Kubernetes 的擴展模式，讓你可以用「宣告式」的方式管理複雜的應用程式。

### 簡單比喻

想像你請了一個專業的 DBA（資料庫管理員）：

- **沒有 Operator**：你要自己寫腳本處理資料庫的安裝、備份、升級、故障轉移...
- **有 Operator**：你只要告訴 DBA「我要一個 3 節點的 MariaDB 叢集」，DBA 就會幫你處理所有細節

Operator 就是這個「自動化的 DBA」。

## Operator 的組成

```
Operator = CRD + Controller
```

| 組件 | 功能 | 比喻 |
|------|------|------|
| CRD | 定義「你想要什麼」的格式 | 點餐單的格式 |
| Controller | 負責「實現你想要的」 | 廚師 |

### CRD (Custom Resource Definition)

CRD 擴展了 Kubernetes API，讓你可以建立自定義資源。

```yaml
# 這是 MariaDB CRD 讓你可以寫的
apiVersion: k8s.mariadb.com/v1alpha1
kind: MariaDB
metadata:
  name: my-database
spec:
  replicas: 3
  storage:
    size: 10Gi
```

原本 Kubernetes 不認識 `MariaDB` 這種資源，安裝 CRD 後就認識了。

### Controller

Controller 是一個持續運行的程式，負責：

1. **監聽** CRD 的變化（有人建立/修改/刪除 MariaDB 資源）
2. **比較** 目前狀態 vs 期望狀態
3. **執行** 必要的操作讓兩者一致

## 為什麼需要 Operator？

### 傳統方式的問題

```bash
# 手動部署 MariaDB
kubectl apply -f mariadb-statefulset.yaml
kubectl apply -f mariadb-service.yaml
kubectl apply -f mariadb-configmap.yaml
kubectl apply -f mariadb-secret.yaml
# 還要處理備份、監控、故障轉移...
```

問題：
- 要記住很多 YAML 檔案
- 升級很複雜
- 故障處理要人工介入

### Operator 方式

```yaml
# 只需要一個檔案
apiVersion: k8s.mariadb.com/v1alpha1
kind: MariaDB
metadata:
  name: my-database
spec:
  replicas: 3
  storage:
    size: 10Gi
  backup:
    schedule: "0 2 * * *"  # 每天凌晨 2 點備份
```

Operator 會自動：
- 建立 StatefulSet、Service、ConfigMap、Secret
- 設定定時備份
- 處理故障轉移
- 管理升級流程

## mariadb-operator 的架構

```
使用者建立 MariaDB CR
        │
        ▼
┌─────────────────────┐
│  MariaDB Controller │ ◄─── 監聽 MariaDB 資源
└─────────────────────┘
        │
        │ 呼叫各種 Sub-reconciler
        ▼
┌─────────────────────────────────────┐
│  StatefulSet Reconciler             │
│  Service Reconciler        ◄─── Modified here
│  ConfigMap Reconciler               │
│  Secret Reconciler                  │
│  Backup Reconciler                  │
│  ...                                │
└─────────────────────────────────────┘
        │
        ▼
   Kubernetes API
  (建立/更新實際資源)
```

## 實際程式碼對應

| 概念 | 程式碼位置 |
|------|-----------|
| CRD 定義 | `api/v1alpha1/*.go` |
| Controller | `internal/controller/*.go` |
| Sub-reconciler | `pkg/controller/*.go` |
| 資源建構器 | `pkg/builder/*.go` |

## 重點整理

1. **Operator = CRD + Controller**
2. **宣告式管理**：你說「要什麼」，Operator 負責「怎麼做」
3. **自動化維運**：備份、升級、故障處理都自動化
4. **領域知識封裝**：把 DBA 的知識寫成程式碼
