# Go 指標在 API 設計中的應用

## 為什麼用 `*string` 而不是 `string`？

在 Kubernetes API 設計中，你會看到很多指標類型：

```go
type ServiceTemplate struct {
    LoadBalancerClass *string `json:"loadBalancerClass,omitempty"`  // 指標
    Type              string  `json:"type,omitempty"`               // 值
}
```

這不是隨便選的，有特殊用意。

## 核心問題：區分「沒設定」vs「設定為空」

### 使用值類型 (string)

```go
type Config struct {
    Name string `json:"name,omitempty"`
}

// YAML 沒有寫 name
c := Config{}
fmt.Println(c.Name)        // ""（空字串）
fmt.Println(c.Name == "")  // true

// YAML 寫了 name: ""
c := Config{Name: ""}
fmt.Println(c.Name)        // ""（空字串）
fmt.Println(c.Name == "")  // true

// 問題：無法區分這兩種情況！
```

### 使用指標類型 (*string)

```go
type Config struct {
    Name *string `json:"name,omitempty"`
}

// YAML 沒有寫 name
c := Config{}
fmt.Println(c.Name)       // nil
fmt.Println(c.Name == nil) // true ← 沒設定

// YAML 寫了 name: ""
empty := ""
c := Config{Name: &empty}
fmt.Println(c.Name)       // 指向 ""
fmt.Println(*c.Name)      // ""
fmt.Println(c.Name == nil) // false ← 有設定（只是值是空的）
```

## 三種狀態

| 狀態 | 值類型 (string) | 指標類型 (*string) |
|------|----------------|-------------------|
| 沒設定 | `""` | `nil` |
| 設定為空 | `""` | `ptr to ""` |
| 設定為值 | `"value"` | `ptr to "value"` |

值類型只能區分 2 種狀態，指標可以區分 3 種。

## 什麼時候用指標？

### 用指標 `*T` 的情況

1. **欄位是可選的，且需要區分「沒設定」**
   ```go
   // 使用者沒設定 = 使用系統預設
   // 使用者設定為 0 = 真的要 0
   Replicas *int32
   ```

2. **Boolean 需要三態**
   ```go
   // nil = 使用預設行為
   // true = 啟用
   // false = 停用
   AllocateLoadBalancerNodePorts *bool
   ```

3. **參考其他資源（可能不存在）**
   ```go
   SecretRef *corev1.LocalObjectReference
   ```

### 用值類型 `T` 的情況

1. **欄位是必填的**
   ```go
   Name string  // 必須有名字
   ```

2. **空值有意義**
   ```go
   // ClusterIP 是合法的值
   Type corev1.ServiceType
   ```

## 程式碼中的處理

### 檢查是否設定

```go
// 指標：檢查 nil
if opts.LoadBalancerClass != nil {
    svc.Spec.LoadBalancerClass = opts.LoadBalancerClass
}

// 值：檢查空值（但無法區分「沒設定」）
if opts.Type != "" {
    svc.Spec.Type = opts.Type
}
```

### 取值

```go
// 指標：需要解參考
var class string
if opts.LoadBalancerClass != nil {
    class = *opts.LoadBalancerClass  // 解參考
}

// 值：直接使用
class := opts.Type
```

### 設定值

```go
// 指標：需要取地址
value := "tailscale"
opts.LoadBalancerClass = &value  // 取地址

// 或用 helper function
func ptr[T any](v T) *T { return &v }
opts.LoadBalancerClass = ptr("tailscale")

// 值：直接賦值
opts.Type = "LoadBalancer"
```

## omitempty 的行為

`json:"xxx,omitempty"` 會在序列化時省略「零值」：

| 類型 | 零值 | omitempty 效果 |
|------|------|---------------|
| `string` | `""` | 空字串會省略 |
| `int` | `0` | 0 會省略 |
| `bool` | `false` | false 會省略 |
| `*string` | `nil` | nil 會省略 |
| `[]T` | `nil` | nil 會省略 |

```go
type Config struct {
    Name  string  `json:"name,omitempty"`
    Count *int    `json:"count,omitempty"`
}

c := Config{Name: "", Count: nil}
// JSON: {}  ← 兩個欄位都省略了

i := 0
c := Config{Name: "", Count: &i}
// JSON: {"count": 0}  ← count 有值所以不省略
```

## 實際應用：ServiceTemplate

```go
type ServiceTemplate struct {
    // 必填欄位或有預設值，用值類型
    Type corev1.ServiceType `json:"type,omitempty"`

    // 可選欄位，需要區分「沒設定」，用指標
    LoadBalancerIP                *string `json:"loadBalancerIP,omitempty"`
    AllocateLoadBalancerNodePorts *bool   `json:"allocateLoadBalancerNodePorts,omitempty"`
    LoadBalancerClass             *string `json:"loadBalancerClass,omitempty"`
}
```

## 重點整理

1. **指標可以區分三種狀態**：nil、空值、有值
2. **可選欄位用指標**：`*string`、`*bool`、`*int32`
3. **必填欄位用值**：`string`、`bool`、`int32`
4. **檢查指標用 `!= nil`**，不是檢查空值
5. **`omitempty` 會省略零值和 nil**
