# Issue #1496 - Container Lifecycle

**Status**: PR Ready (未提交)
**Date**: 2026-01-22
**Branch**: `feat-container-lifecycle`

## Problem

用戶需要在 MariaDB Pod 關閉前執行 `PreStop` lifecycle hook，讓 Kubernetes 先排空流量，等現有連線結束後再關閉 pod。

## What I Learned

### 1. Kubernetes Lifecycle Hooks

```yaml
spec:
  containers:
  - name: app
    lifecycle:
      postStart:          # 容器啟動後執行
        exec:
          command: ["/bin/sh", "-c", "echo started"]
      preStop:            # 容器終止前執行
        exec:
          command: ["/bin/sh", "-c", "sleep 10"]
```

| Hook | 用途 | 場景 |
|------|------|------|
| `postStart` | 容器啟動後執行 | 初始化、通知其他服務 |
| `preStop` | 容器終止前執行 | **優雅關閉**、排空連線 |

### 2. Pod 終止流程

```
1. Kubernetes 收到終止信號
2. Pod 狀態變成 Terminating
3. Service 移除該 Pod 的 endpoint（新流量不再進入）
4. 執行 PreStop hook（給時間讓現有連線完成）
5. 發送 SIGTERM 給容器
6. 等待 terminationGracePeriodSeconds
7. 強制 SIGKILL
```

### 3. 直接使用 Kubernetes 類型 vs 自定義類型

這個 codebase 有兩種模式：

| 模式 | 範例 | 說明 |
|------|------|------|
| 自定義類型 | `SecurityContext`, `Probe` | 需要 `ToKubernetesType()` 轉換 |
| 直接用 K8s 類型 | `Lifecycle` | 結構簡單，不需轉換 |

選擇 `corev1.Lifecycle` 因為：
- 結構已經標準化
- 不需要額外的轉換函數
- 程式碼更簡潔

### 4. PreStop 時間選擇

**重要限制**：
```
PreStop 時間 < terminationGracePeriodSeconds
```

如果 PreStop 超時，Kubernetes 會強制 SIGKILL！

| 場景 | PreStop | terminationGracePeriodSeconds |
|------|---------|-------------------------------|
| 簡單 API | 5-10s | 30s |
| 資料庫（短查詢） | 10-15s | 60s |
| 資料庫（長查詢） | 30-60s | 120s |

## Implementation Steps

### Step 1: 定義 API 欄位

**File**: `api/v1alpha1/base_types.go`

```go
type ContainerTemplate struct {
    // ... 其他欄位 ...

    // Lifecycle describes actions the management system should take
    // in response to container lifecycle events.
    // +optional
    // +operator-sdk:csv:customresourcedefinitions:type=spec
    Lifecycle *corev1.Lifecycle `json:"lifecycle,omitempty"`
}
```

### Step 2: 在 Builder 中套用

**File**: `pkg/builder/container_builder.go`

```go
container := corev1.Container{
    // ... 其他欄位 ...
    Lifecycle: tpl.Lifecycle,  // 直接賦值，不需轉換！
}
```

### Step 3: 執行程式碼生成

```bash
make gen
```

### Step 4: 新增測試

**File**: `pkg/builder/container_builder_test.go`

測試兩種情況：
1. 沒設定 → 應該是 nil
2. 有設定 → 應該正確傳遞

## Files Modified

| File | Changes |
|------|---------|
| `api/v1alpha1/base_types.go` | 新增 `Lifecycle` 欄位 |
| `pkg/builder/container_builder.go` | 套用 `Lifecycle` |
| `pkg/builder/container_builder_test.go` | 新增測試 |
| Generated files | 自動生成 |

## Data Flow

```
User's MariaDB YAML
       │
       ▼
ContainerTemplate.Lifecycle
       │
       ▼ (Builder)
container.Lifecycle = tpl.Lifecycle
       │
       ▼
Pod Spec with lifecycle hooks
```

## Key Insight

這個 issue 比 #1376 (loadBalancerClass) 更簡單：
- loadBalancerClass 需要改 Reconciler 來同步 Service
- Lifecycle 只需改 API + Builder，因為 ContainerTemplate 會被 StatefulSet 直接使用

## Commands Used

```bash
# 建立分支
git checkout -b feat-container-lifecycle

# 生成程式碼
make gen

# 執行測試
go test ./pkg/builder/... -v -run TestContainerLifecycle

# 執行 lint
make lint

# Commit
git commit -m "feat: add lifecycle field to ContainerTemplate"
```
