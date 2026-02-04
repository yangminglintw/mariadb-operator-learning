# Kubebuilder Markers（標記）

## 什麼是 Markers？

Markers 是 Go 註解中以 `+` 開頭的特殊標記，用來控制程式碼產生。

```go
// +optional                    ◄── 這是 marker
// +kubebuilder:validation:Minimum=1
LoadBalancerClass *string `json:"loadBalancerClass,omitempty"`
```

Kubebuilder 工具會讀取這些標記，自動產生：
- CRD YAML（OpenAPI schema）
- DeepCopy 方法
- RBAC 設定

## 常用 Markers

### 1. 欄位可選性

```go
// +optional
LoadBalancerClass *string `json:"loadBalancerClass,omitempty"`
```

表示這個欄位不是必填。在 CRD 中不會出現在 `required` 列表。

### 2. 驗證規則

```go
// +kubebuilder:validation:Minimum=1
// +kubebuilder:validation:Maximum=100
Replicas int32 `json:"replicas"`

// +kubebuilder:validation:Enum=ClusterIP;NodePort;LoadBalancer
Type corev1.ServiceType `json:"type"`

// +kubebuilder:validation:Pattern=`^[a-z]+$`
Name string `json:"name"`
```

| Marker | 用途 | 例子 |
|--------|------|------|
| `Minimum` | 最小值 | `Minimum=1` |
| `Maximum` | 最大值 | `Maximum=100` |
| `Enum` | 限定選項 | `Enum=A;B;C` |
| `Pattern` | 正則表達式 | `Pattern=^[a-z]+$` |
| `MinLength` | 最小長度 | `MinLength=1` |
| `MaxLength` | 最大長度 | `MaxLength=256` |

### 3. 預設值

```go
// +kubebuilder:default=3
Replicas int32 `json:"replicas"`

// +kubebuilder:default=ClusterIP
Type corev1.ServiceType `json:"type"`
```

### 4. Operator SDK 標記

```go
// +operator-sdk:csv:customresourcedefinitions:type=spec
// +operator-sdk:csv:customresourcedefinitions:type=status
// +operator-sdk:csv:customresourcedefinitions:xDescriptors={"urn:alm:descriptor:com.tectonic.ui:advanced"}
```

這些用於 OLM（Operator Lifecycle Manager）的 CSV 產生。

| xDescriptor | 用途 |
|-------------|------|
| `advanced` | 顯示在進階設定區塊 |
| `booleanSwitch` | 顯示為開關 |
| `password` | 顯示為密碼欄位 |
| `number` | 顯示為數字輸入 |

### 5. 物件層級標記

```go
// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Ready",type="string",JSONPath=".status.conditions[0].status"
type MariaDB struct {
    metav1.TypeMeta   `json:",inline"`
    metav1.ObjectMeta `json:"metadata,omitempty"`
    Spec   MariaDBSpec   `json:"spec,omitempty"`
    Status MariaDBStatus `json:"status,omitempty"`
}
```

| Marker | 用途 |
|--------|------|
| `object:root=true` | 這是一個 root 物件（CRD） |
| `subresource:status` | 啟用 status 子資源 |
| `printcolumn` | `kubectl get` 時顯示的欄位 |

## 實際範例解析

mariadb-operator 中 `ServiceTemplate` 的 markers：

```go
type ServiceTemplate struct {
    // Type is the Service type.
    // +optional
    // +kubebuilder:default=ClusterIP
    // +kubebuilder:validation:Enum=ClusterIP;NodePort;LoadBalancer
    // +operator-sdk:csv:customresourcedefinitions:type=spec,xDescriptors={"urn:alm:descriptor:com.tectonic.ui:advanced"}
    Type corev1.ServiceType `json:"type,omitempty"`

    // AllocateLoadBalancerNodePorts Service field.
    // +optional
    // +operator-sdk:csv:customresourcedefinitions:type=spec,xDescriptors={"urn:alm:descriptor:com.tectonic.ui:booleanSwitch","urn:alm:descriptor:com.tectonic.ui:advanced"}
    AllocateLoadBalancerNodePorts *bool `json:"allocateLoadBalancerNodePorts,omitempty"`

    // LoadBalancerClass Service field.
    // +optional
    // +operator-sdk:csv:customresourcedefinitions:type=spec,xDescriptors={"urn:alm:descriptor:com.tectonic.ui:advanced"}
    LoadBalancerClass *string `json:"loadBalancerClass,omitempty"`
}
```

分析：
- `Type` 欄位：可選、預設 ClusterIP、只能是三種值之一
- `AllocateLoadBalancerNodePorts`：可選、顯示為開關、在進階區塊
- `LoadBalancerClass`：可選、在進階區塊

## CRD YAML 產生結果

執行 `make manifests` 後，markers 會轉換成 OpenAPI schema：

```yaml
# config/crd/bases/k8s.mariadb.com_mariadbs.yaml
spec:
  properties:
    service:
      properties:
        type:
          type: string
          default: ClusterIP                    # +kubebuilder:default
          enum:                                 # +kubebuilder:validation:Enum
            - ClusterIP
            - NodePort
            - LoadBalancer
        loadBalancerClass:
          type: string
        allocateLoadBalancerNodePorts:
          type: boolean
```

## 常見錯誤

### 1. Marker 拼錯

```go
// 錯誤
// +opitonal  ◄── 拼錯了

// 正確
// +optional
```

### 2. Marker 沒有緊鄰欄位

```go
// 錯誤：中間有空行
// +optional

LoadBalancerClass *string

// 正確：緊鄰欄位
// +optional
LoadBalancerClass *string
```

### 3. Enum 值分隔符錯誤

```go
// 錯誤：用逗號
// +kubebuilder:validation:Enum=A,B,C

// 正確：用分號
// +kubebuilder:validation:Enum=A;B;C
```

## 重點整理

1. **Markers 控制程式碼產生**（CRD、RBAC 等）
2. **`+optional`**：最常用，表示欄位非必填
3. **`+kubebuilder:validation:*`**：輸入驗證
4. **`+kubebuilder:default=*`**：設定預設值
5. **`+operator-sdk:csv:*`**：OLM 相關
6. **修改後要執行 `make manifests`**
