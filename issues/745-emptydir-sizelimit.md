# Issue #745 - emptyDir sizeLimit

**Status**: PR #1594 Created
**Date**: 2026-01-22
**Branch**: `feat-emptydir-sizelimit`

## Problem

用戶的 K8s 叢集有 Kyverno policies，要求 emptyDir 必須設定 sizeLimit。目前 operator 使用 `corev1.EmptyDirVolumeSource{}` 沒有提供設定選項。

## What I Learned

### 1. Kubernetes emptyDir Volume

```yaml
volumes:
- name: temp-storage
  emptyDir:
    medium: ""           # 預設用硬碟，"Memory" 用 RAM
    sizeLimit: "500Mi"   # 容量限制
```

| 特點 | 說明 |
|------|------|
| 生命週期 | Pod 刪除時清空 |
| medium | `""` = 硬碟, `"Memory"` = RAM |
| sizeLimit | 容量限制（Kyverno 政策常要求） |

### 2. 重用現有類型

這個 codebase 已經有 `EmptyDirVolumeSource` 類型：

```go
// api/v1alpha1/kubernetes_volume_types.go
type EmptyDirVolumeSource struct {
    Medium    corev1.StorageMedium `json:"medium,omitempty"`
    SizeLimit *resource.Quantity   `json:"sizeLimit,omitempty"`
}

func (v EmptyDirVolumeSource) ToKubernetesType() corev1.EmptyDirVolumeSource {
    return corev1.EmptyDirVolumeSource{
        Medium:    v.Medium,
        SizeLimit: v.SizeLimit,
    }
}
```

**學習點**：先搜尋現有類型，不要重新發明輪子！

### 3. Issue 討論的重要性

維護者 @mmontes11 在 issue 中提供了：
- 所有需要修改的程式碼位置
- YAML API 範例
- 部分已由 PR #946 完成

**學習點**：開始 coding 前，先讀完所有 issue comments！

### 4. nil 檢查模式

```go
// 如果用戶沒設定，用預設值
emptyDir := &corev1.EmptyDirVolumeSource{}
if mariadb.Spec.Storage.EmptyDir != nil {
    emptyDir = ptr.To(mariadb.Spec.Storage.EmptyDir.ToKubernetesType())
}
```

這個模式確保：
- 向後相容（沒設定 = 預設行為）
- 有設定時使用用戶的值

## Implementation

### Files Modified

| File | Changes |
|------|---------|
| `api/v1alpha1/mariadb_types.go` | Storage 新增 EmptyDir 欄位 |
| `api/v1alpha1/maxscale_types.go` | MaxScaleConfig 新增 EmptyDir 欄位 |
| `pkg/builder/pod_builder.go` | 套用 emptyDir 設定 |
| `pkg/builder/pod_builder_test.go` | 新增測試 |

### API Design

**MariaDB:**
```yaml
spec:
  storage:
    ephemeral: true
    emptyDir:
      sizeLimit: 1Gi
      medium: Memory  # optional
```

**MaxScale:**
```yaml
spec:
  config:
    emptyDir:
      sizeLimit: 500Mi
```

### Data Flow

```
User YAML
    │
    ▼
Storage.EmptyDir / Config.EmptyDir
    │
    ▼ (nil check)
if EmptyDir != nil {
    use user's config
} else {
    use default
}
    │
    ▼
Pod Volume with emptyDir
```

## Key Insights

1. **先讀 issue 討論** - 維護者可能已經提供了詳細指引
2. **搜尋現有類型** - `EmptyDirVolumeSource` 已經存在，直接用
3. **nil 檢查模式** - 確保向後相容
4. **一個設定套用多個 volume** - MaxScale 的 3 個 emptyDir 共用一個設定

## Commands Used

```bash
# 建立分支
git checkout -b feat-emptydir-sizelimit

# 生成程式碼
make gen

# 執行測試
go test ./pkg/builder/... -v -run "TestMariadbEphemeralStorageEmptyDir|TestMaxScaleEmptyDir"

# 執行 lint
make lint

# Commit
git commit -m "feat: add emptyDir configuration for MariaDB and MaxScale"

# Push
git push -u origin feat-emptydir-sizelimit
```
