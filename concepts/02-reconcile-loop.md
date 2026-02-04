# Reconcile Loop（調和迴圈）

## 什麼是 Reconcile？

Reconcile 的意思是「調和」或「使一致」。

Controller 的核心工作就是不斷地做 Reconcile：
**讓「實際狀態」趨近於「期望狀態」**

## 簡單比喻

想像你是一個恆溫器：

```
期望溫度：25°C（你設定的）
實際溫度：22°C（房間現在的溫度）

Reconcile 動作：開暖氣
```

每隔一段時間檢查一次，如果溫度不對就調整。這就是 Reconcile Loop。

## Reconcile 的流程

```
          ┌─────────────────────┐
          │  事件觸發            │
          │  (CR 建立/修改/刪除) │
          └──────────┬──────────┘
                     ▼
          ┌─────────────────────┐
          │  取得期望狀態        │
          │  (從 CR spec 讀取)  │
          └──────────┬──────────┘
                     ▼
          ┌─────────────────────┐
          │  取得實際狀態        │
          │  (從 K8s API 查詢)  │
          └──────────┬──────────┘
                     ▼
          ┌─────────────────────┐
          │  比較差異            │
          └──────────┬──────────┘
                     ▼
              ┌──────┴──────┐
              │             │
        相同 ▼             ▼ 不同
      ┌──────────┐    ┌──────────┐
      │ 不做事    │    │ 執行修補  │
      │ 回傳成功  │    │ (Patch)  │
      └──────────┘    └──────────┘
```

## 程式碼範例

Service Reconciler example:

```go
func (r *ServiceReconciler) Reconcile(ctx context.Context, desiredSvc *corev1.Service) error {
    // 1. 取得期望狀態（參數傳入的 desiredSvc）

    // 2. 取得實際狀態
    key := client.ObjectKeyFromObject(desiredSvc)
    var existingSvc corev1.Service
    if err := r.Get(ctx, key, &existingSvc); err != nil {
        if !apierrors.IsNotFound(err) {
            return fmt.Errorf("error getting Service: %v", err)
        }
        // 不存在就建立
        return r.Create(ctx, desiredSvc)
    }

    // 3. 比較並修補差異
    patch := client.MergeFrom(existingSvc.DeepCopy())

    // 把期望的欄位複製到實際的物件上
    existingSvc.Spec.Type = desiredSvc.Spec.Type
    existingSvc.Spec.Selector = desiredSvc.Spec.Selector
    existingSvc.Spec.LoadBalancerClass = desiredSvc.Spec.LoadBalancerClass  // Added field

    // 4. 套用修補
    return r.Patch(ctx, &existingSvc, patch)
}
```

## 重要觀念

### 1. 冪等性 (Idempotent)

Reconcile 必須是「冪等」的，意思是：
**執行一次和執行一百次的結果要一樣**

```go
// 好的寫法：冪等
existingSvc.Spec.Type = desiredSvc.Spec.Type  // 設定成期望值

// 不好的寫法：不冪等
existingSvc.Spec.Replicas++  // 每次執行都會增加！
```

### 2. Level-triggered vs Edge-triggered

Kubernetes Controller 使用 **Level-triggered**（狀態觸發）：

| 類型 | 說明 | 例子 |
|------|------|------|
| Edge-triggered | 只在「變化時」觸發 | 門鈴：按下才響 |
| Level-triggered | 只要「狀態不對」就觸發 | 恆溫器：溫度不對就調整 |

好處：即使錯過事件，下次 Reconcile 還是會修正。

### 3. 為什麼用 Patch 而不是 Update？

```go
// Patch：只更新有改變的欄位
patch := client.MergeFrom(existingSvc.DeepCopy())
r.Patch(ctx, &existingSvc, patch)

// Update：整個物件覆蓋（容易造成衝突）
r.Update(ctx, &existingSvc)
```

Patch 的好處：
- 減少衝突（其他 Controller 可能同時在改）
- 只傳送差異，效率較高

## 常見的 Reconcile 模式

### Create or Update Pattern

```go
// 嘗試取得
err := r.Get(ctx, key, &existing)
if apierrors.IsNotFound(err) {
    // 不存在：建立
    return r.Create(ctx, desired)
}
if err != nil {
    return err
}
// 存在：更新
return r.Patch(ctx, &existing, patch)
```

### 建立子資源 Pattern

```go
func (r *MariaDBReconciler) Reconcile(ctx context.Context, req ctrl.Request) {
    // 1. 取得 MariaDB CR
    var mariadb v1alpha1.MariaDB
    r.Get(ctx, req.NamespacedName, &mariadb)

    // 2. 建構期望的 Service
    desiredSvc := r.builder.BuildService(&mariadb)

    // 3. 調和 Service
    r.serviceReconciler.Reconcile(ctx, desiredSvc)

    // 4. 調和其他子資源...
    r.statefulSetReconciler.Reconcile(ctx, desiredSts)
}
```

## 重點整理

1. **Reconcile = 讓實際狀態趨近期望狀態**
2. **必須冪等**：執行多次結果一樣
3. **Level-triggered**：狀態不對就修正
4. **用 Patch 不用 Update**：減少衝突
