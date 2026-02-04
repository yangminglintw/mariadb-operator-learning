# CRD 新增欄位的模式

## 概述

在 mariadb-operator 中，當你要新增一個欄位讓使用者可以設定，需要修改三個地方：

```
API (定義) → Builder (建構) → Reconciler (同步)
```

## 三步驟詳解

### Step 1: API 定義

在 `api/v1alpha1/` 目錄下的 Go struct 新增欄位。

**檔案位置**：`api/v1alpha1/base_types.go`

```go
type ServiceTemplate struct {
    // ... 其他欄位 ...

    // LoadBalancerClass Service field.
    // +optional
    // +operator-sdk:csv:customresourcedefinitions:type=spec
    LoadBalancerClass *string `json:"loadBalancerClass,omitempty"`
}
```

這讓使用者可以在 YAML 中寫：

```yaml
apiVersion: k8s.mariadb.com/v1alpha1
kind: MariaDB
metadata:
  name: my-db
spec:
  service:
    loadBalancerClass: "tailscale"  # 新欄位！
```

### Step 2: Builder 建構

Builder 負責把 CR 的設定轉換成實際的 Kubernetes 資源。

**檔案位置**：`pkg/builder/service_builder.go`

```go
func (b *Builder) BuildService(key types.NamespacedName, owner metav1.Object, opts ServiceOpts) (*corev1.Service, error) {
    svc := &corev1.Service{
        // ... 基本設定 ...
    }

    // 如果使用者有設定 LoadBalancerClass，就加到 Service 上
    if opts.LoadBalancerClass != nil {
        svc.Spec.LoadBalancerClass = opts.LoadBalancerClass
    }

    return svc, nil
}
```

### Step 3: Reconciler 同步

Reconciler 負責在更新時同步欄位。

**檔案位置**：`pkg/controller/service/controller.go`

```go
func (r *ServiceReconciler) Reconcile(ctx context.Context, desiredSvc *corev1.Service) error {
    // ... 取得現有 Service ...

    // 同步欄位
    existingSvc.Spec.LoadBalancerClass = desiredSvc.Spec.LoadBalancerClass

    // ... Patch 更新 ...
}
```

## 為什麼需要三個地方？

```
                    使用者的 YAML
                         │
                         ▼
┌──────────────────────────────────────────┐
│ Step 1: API                              │
│ 定義欄位，讓 K8s 認識這個設定              │
│ 產生 CRD YAML                             │
└────────────────────┬─────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────┐
│ Step 2: Builder                          │
│ 建立新資源時，把設定值帶到實際資源上         │
│ MariaDB CR → Service 物件                 │
└────────────────────┬─────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────┐
│ Step 3: Reconciler                       │
│ 更新現有資源時，同步欄位值                  │
│ 確保期望狀態 = 實際狀態                    │
└──────────────────────────────────────────┘
```

## 程式碼產生

修改 API 後，需要執行程式碼產生：

```bash
# 產生 DeepCopy 方法
make code

# 產生 CRD YAML 檔案
make manifests
```

### 為什麼需要 DeepCopy？

Kubernetes controller 經常需要複製物件（避免修改到原始資料）：

```go
// 這會修改到原始物件！危險！
patch := client.MergeFrom(existingSvc)

// 這才安全：複製一份
patch := client.MergeFrom(existingSvc.DeepCopy())
```

`make code` 會自動產生 `DeepCopy()` 方法到 `zz_generated.deepcopy.go`。

### CRD YAML 在哪？

```
config/crd/bases/
├── k8s.mariadb.com_mariadbs.yaml    # MariaDB 的 CRD
└── k8s.mariadb.com_maxscales.yaml   # MaxScale 的 CRD
```

執行 `make manifests` 後，新欄位會出現在這些 YAML 中：

```yaml
# config/crd/bases/k8s.mariadb.com_mariadbs.yaml
properties:
  service:
    properties:
      loadBalancerClass:    # New field
        type: string
```

## 完整流程圖

```
使用者修改 MariaDB CR
    spec.service.loadBalancerClass: "tailscale"
                │
                ▼
        Kubernetes API
        (因為有 CRD 所以認識這個欄位)
                │
                ▼
      MariaDB Controller 收到事件
                │
                ▼
      呼叫 Builder.BuildService()
      把 loadBalancerClass 放進 Service
                │
                ▼
      呼叫 ServiceReconciler.Reconcile()
                │
        ┌───────┴───────┐
        ▼               ▼
    Service 不存在    Service 已存在
        │               │
        ▼               ▼
      Create          Patch
    (整個建立)     (同步 loadBalancerClass)
                │
                ▼
         實際的 Service 被更新
         spec.loadBalancerClass: "tailscale"
```

## 檢查清單

新增欄位時，確認以下事項：

- [ ] API struct 有加欄位和適當的 tag
- [ ] Builder 有處理這個欄位（if opts.XXX != nil）
- [ ] Reconciler 有同步這個欄位
- [ ] 執行 `make code`
- [ ] 執行 `make manifests`
- [ ] 執行 `make lint`
- [ ] CRD YAML 中有出現新欄位

## 常見錯誤

### 1. 忘記在 Reconciler 同步

```go
// 只在 Builder 設定，忘記 Reconciler
// 結果：建立時有值，但更新時不會同步！
```

### 2. 沒有執行 make code

```
錯誤訊息：undefined: SomeType.DeepCopy
原因：DeepCopy 方法還沒產生
解法：make code
```

### 3. 欄位名稱不一致

```go
// API 定義
LoadBalancerClass *string `json:"loadBalancerClass"`

// YAML 要寫
loadBalancerClass: "xxx"  # 小寫開頭，因為 json tag

// 不是
LoadBalancerClass: "xxx"  # 錯！
```
