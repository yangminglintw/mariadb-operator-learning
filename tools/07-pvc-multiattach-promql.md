# PVC Multi-Attach Error PromQL 監控

## 問題背景

### 什麼是 Multi-Attach Error

```
情境：MariaDB StatefulSet (3 replicas) 跑在 Kubernetes 上

Node A (NotReady) ── mariadb-chaos-1 pod (Terminating，但 kubelet 沒回應)
                              │
                              ▼ RWO PVC（還掛在 Node A）
                              │
Node B ── mariadb-chaos-1 pod (ContainerCreating，等 volume detach)
                              └── Multi-Attach error！

結果：新 pod 卡在 ContainerCreating，直到舊 node 上的 volume 被強制 detach
```

### 為什麼 StatefulSet 特別容易遇到

| 特性 | 說明 |
|------|------|
| Stable identity | Pod 名稱固定（`mariadb-0`, `mariadb-1`...），重建時用同一個 PVC |
| RWO volume | ReadWriteOnce 只能掛載到一個 node |
| 非 graceful shutdown | Node NotReady 時 kubelet 無法正常 unmount |

### 時間線

```
T+0s    Node A 變成 NotReady（kubelet 失聯）
T+40s   Node controller 標記 pod 為 Terminating（但 kubelet 不回應，pod 不會真的刪除）
T+5m    StatefulSet controller 嘗試在 Node B 建立新 pod
T+5m    新 pod 進入 ContainerCreating
T+5m    kubelet 嘗試 attach volume → Multi-Attach error（volume 還在 Node A）
T+6m    默認等待 maxWaitForUnmountDuration (6 min) 後強制 detach
T+11m+  Volume 終於掛載成功，pod 啟動
```

**關鍵**：Multi-Attach error 是 **Kubernetes Event**，不是直接的 Prometheus metric。
需要組合多個 kube-state-metrics 指標來偵測症狀和前兆。

---

## Kubernetes 物件關聯鏈

### 完整關係圖

```
Pod
 │ spec.volumes[].persistentVolumeClaim.claimName
 ▼
PVC (PersistentVolumeClaim)
 │ spec.volumeName
 ▼
PV (PersistentVolume)
 │ 被 VolumeAttachment 引用
 ▼
VolumeAttachment
 │ spec.source.persistentVolumeName = PV name
 │ spec.nodeName = 掛載到哪個 node
 │ status.attached = true/false
 ▼
Node
```

### kubectl 追蹤指令

```bash
# Step 1: Pod → PVC
kubectl --context=kind-mdb get pod mariadb-chaos-1 \
  -o jsonpath='{.spec.volumes[*].persistentVolumeClaim.claimName}'

# Step 2: PVC → PV
kubectl --context=kind-mdb get pvc <pvc-name> \
  -o jsonpath='{.spec.volumeName}'

# Step 3: PV → VolumeAttachment
kubectl --context=kind-mdb get volumeattachment \
  -o custom-columns='NAME:.metadata.name,PV:.spec.source.persistentVolumeName,NODE:.spec.nodeName' \
  | grep <pv-name>

# Step 4: 一次看全部 VolumeAttachment
kubectl --context=kind-mdb get volumeattachment \
  -o custom-columns='NAME:.metadata.name,PV:.spec.source.persistentVolumeName,NODE:.spec.nodeName,ATTACHED:.status.attached'
```

---

## kube-state-metrics 可用指標

### 指標總覽

| 指標 | 重要 Labels | 用途 |
|------|-------------|------|
| `kube_node_status_condition` | node, condition, status | 偵測 Node NotReady |
| `kube_pod_container_status_waiting_reason` | pod, namespace, container, reason | 偵測 ContainerCreating |
| `kube_pod_status_phase` | pod, namespace, phase | Pod 狀態（Pending/Running） |
| `kube_pod_spec_volumes_persistentvolumeclaims_info` | pod, namespace, persistentvolumeclaim | Pod → PVC 對應 |
| `kube_persistentvolumeclaim_info` | persistentvolumeclaim, namespace, storageclass, volumename | PVC → PV 對應 |
| `kube_persistentvolumeclaim_status_phase` | persistentvolumeclaim, namespace, phase | PVC 狀態（Bound/Pending） |
| `kube_volumeattachment_spec_source_persistentvolume` | volumeattachment, volumename | **Bridge metric**：PV → VA |
| `kube_volumeattachment_status_attached` | volumeattachment, node | VA 掛載狀態（0/1） |
| `kube_volumeattachment_info` | volumeattachment, attacher, node | VA 基本資訊 |

### 指標值說明

```
kube_node_status_condition{condition="Ready", status="true"} == 1   → Node 正常
kube_node_status_condition{condition="Ready", status="true"} == 0   → Node NotReady

kube_volumeattachment_status_attached == 1   → Volume 已掛載
kube_volumeattachment_status_attached == 0   → Volume 未掛載

kube_pod_container_status_waiting_reason{reason="ContainerCreating"} == 1  → Pod 等待中
```

---

## PromQL Label Join 問題

### 核心問題：Label 不對齊

不同 metrics 的 label 名稱不同，無法直接 join：

```
kube_pod_spec_volumes_*              → label: persistentvolumeclaim
kube_persistentvolumeclaim_info      → label: persistentvolumeclaim, volumename
kube_volumeattachment_status_*       → label: volumeattachment, node
                                       ↑ 沒有 persistentvolumeclaim 或 volumename → 斷裂！
```

### 為什麼會斷裂

```
想要的查詢鏈：
  Pod 用了哪個 PVC → PVC 對應哪個 PV → PV 掛在哪個 Node → Node 是否 NotReady

問題：
  kube_volumeattachment_status_attached 只有 volumeattachment 和 node label
  kube_volumeattachment_info 只多了 attacher label
  → 找不到一個指標同時有 PV name 和 volumeattachment → 無法從 PV 跳到 VA！
```

### 解法：Bridge Metric

`kube_volumeattachment_spec_source_persistentvolume` 同時擁有：
- `volumename`（PV 名稱）— 可以和 `kube_persistentvolumeclaim_info` join
- `volumeattachment`（VA 名稱）— 可以和 `kube_volumeattachment_status_attached` join

```
這個指標是 bridge！

kube_pod_spec_volumes_persistentvolumeclaims_info
  │ join on (persistentvolumeclaim, namespace)
  ▼
kube_persistentvolumeclaim_info
  │ join on (volumename)
  ▼
kube_volumeattachment_spec_source_persistentvolume  ← Bridge!
  │ join on (volumeattachment)
  ▼
kube_volumeattachment_status_attached               ← 得到 node label
  │ join on (node)
  ▼
kube_node_status_condition                          ← 判斷 NotReady
```

---

## 實用 PromQL 查詢

### 基本偵測

**Node NotReady：**

```promql
# 哪些 node 不健康
kube_node_status_condition{condition="Ready", status="true"} == 0
```

**Pod 卡在 ContainerCreating 超過 5 分鐘：**

```promql
# 偵測 ContainerCreating 持續超過 5 分鐘的 pod
min_over_time(
  kube_pod_container_status_waiting_reason{reason="ContainerCreating"}[5m]
) == 1
```

**StatefulSet 的 Pod 在 NotReady node 上：**

```promql
# StatefulSet pod 跑在 NotReady node 上（風險前兆）
kube_pod_info{created_by_kind="StatefulSet"}
  * on(node) group_left()
  (kube_node_status_condition{condition="Ready", status="true"} == 0)
```

### 完整 Join：PVC → PV → VolumeAttachment → Node

**Step 1：用 bridge metric 串接 PVC 到 VolumeAttachment**

```promql
# PVC → PV（透過 kube_persistentvolumeclaim_info）
# PV → VA（透過 bridge metric）
# VA → attached + node
kube_volumeattachment_status_attached == 1
  * on(volumeattachment) group_left(volumename)
  kube_volumeattachment_spec_source_persistentvolume
```

**Step 2：加上 PVC 資訊**

```promql
# 完整鏈：VA attached status + PV name + PVC name
(
  kube_volumeattachment_status_attached == 1
    * on(volumeattachment) group_left(volumename)
    kube_volumeattachment_spec_source_persistentvolume
)
  * on(volumename) group_left(persistentvolumeclaim, namespace)
  kube_persistentvolumeclaim_info
```

**Step 3：Volume 掛在 NotReady node 上（Multi-Attach 風險偵測）**

```promql
# 核心告警查詢：volume 還 attached 在一個 NotReady 的 node 上
(
  kube_volumeattachment_status_attached == 1
    * on(volumeattachment) group_left(volumename)
    kube_volumeattachment_spec_source_persistentvolume
)
  * on(node) group_left()
  (kube_node_status_condition{condition="Ready", status="true"} == 0)
```

### PromQL Join 語法說明

```
* on(label) group_left(extra_labels) 的意思：

  左邊 metric * on(共同label) group_left(要從右邊帶過來的label) 右邊 metric

  on(label)        → 用哪個 label 做 join（像 SQL 的 ON）
  group_left(...)  → 從右邊帶額外的 label 到結果中
  *                → 值相乘（兩邊都是 0/1 時，等同於 AND）
```

---

## 告警規則範本

```yaml
groups:
- name: pvc-multiattach-alerts
  rules:

  # ═══════════════════════════════════════════════
  # 告警 1：症狀偵測（最實用，不需要複雜 join）
  # ═══════════════════════════════════════════════
  - alert: PodStuckContainerCreating
    expr: |
      min_over_time(
        kube_pod_container_status_waiting_reason{reason="ContainerCreating"}[5m]
      ) == 1
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "Pod stuck in ContainerCreating"
      description: |
        Pod {{ $labels.namespace }}/{{ $labels.pod }} container {{ $labels.container }}
        has been in ContainerCreating for more than 5 minutes.
        Possible causes: Multi-Attach error, image pull failure, secret not found.

  # ═══════════════════════════════════════════════
  # 告警 2：前兆偵測（Node NotReady + StatefulSet）
  # ═══════════════════════════════════════════════
  - alert: NodeNotReadyWithStatefulSet
    expr: |
      count by (node) (
        kube_pod_info{created_by_kind="StatefulSet"}
          * on(node) group_left()
          (kube_node_status_condition{condition="Ready", status="true"} == 0)
      ) > 0
    for: 1m
    labels:
      severity: critical
    annotations:
      summary: "NotReady node has StatefulSet pods"
      description: |
        Node {{ $labels.node }} is NotReady and has StatefulSet pods.
        These pods use RWO PVC and may encounter Multi-Attach errors
        when rescheduled to another node.

  # ═══════════════════════════════════════════════
  # 告警 3：精確偵測（需要 bridge metric）
  # ═══════════════════════════════════════════════
  - alert: VolumeStuckOnNotReadyNode
    expr: |
      (
        kube_volumeattachment_status_attached == 1
          * on(volumeattachment) group_left(volumename)
          kube_volumeattachment_spec_source_persistentvolume
      )
        * on(node) group_left()
        (kube_node_status_condition{condition="Ready", status="true"} == 0)
    for: 2m
    labels:
      severity: critical
    annotations:
      summary: "Volume attached to NotReady node"
      description: |
        VolumeAttachment {{ $labels.volumeattachment }} (PV: {{ $labels.volumename }})
        is still attached to node {{ $labels.node }} which is NotReady.
        Any pod using this volume on another node will get Multi-Attach error.
```

---

## 監控策略比較

| 方法 | 可行性 | 精確度 | 前置需求 | 適用場景 |
|------|--------|--------|----------|----------|
| 症狀偵測（ContainerCreating > 5min） | 高 | 中 | kube-state-metrics | 通用告警，涵蓋多種原因 |
| 前兆偵測（NotReady + StatefulSet） | 高 | 中 | kube-state-metrics | 提前警告，但可能誤報 |
| Join 到 VolumeAttachment（bridge） | 高 | 高 | kube-state-metrics 有 `spec_source` 指標 | 精確偵測 volume 卡住 |
| Event exporter | 最精確 | 高 | 需額外部署 event-exporter | 捕捉 `FailedAttachVolume` 事件 |

### 建議組合

```
Layer 1（症狀）：PodStuckContainerCreating
  └── 最簡單，覆蓋面廣，5 分鐘內告警

Layer 2（前兆）：NodeNotReadyWithStatefulSet
  └── Node 一 NotReady 就告警，讓 on-call 提前準備

Layer 3（精確）：VolumeStuckOnNotReadyNode
  └── 精確定位哪個 volume 卡住，需要 bridge metric 存在
```

---

## 限制與注意事項

### kube-state-metrics 版本

| 指標 | 最低版本 | 備註 |
|------|---------|------|
| `kube_node_status_condition` | 1.x | 基本指標，所有版本都有 |
| `kube_pod_container_status_waiting_reason` | 1.x | 基本指標 |
| `kube_volumeattachment_*` | 2.x | 較新指標，需確認版本 |
| `kube_volumeattachment_spec_source_persistentvolume` | 2.4+ | Bridge metric，需確認是否啟用 |

確認方式：

```bash
# 檢查 kube-state-metrics 版本
kubectl --context=kind-mdb -n monitoring get deploy kube-state-metrics \
  -o jsonpath='{.spec.template.spec.containers[0].image}'

# 檢查 bridge metric 是否存在（在 Prometheus UI 查詢）
# kube_volumeattachment_spec_source_persistentvolume
# 如果有結果，表示可以用完整 join chain
```

### Metrics 狀態型 vs 累計型

```
本文用到的指標都是「狀態型」（gauge）：
├── kube_node_status_condition = 0 或 1（即時狀態）
├── kube_volumeattachment_status_attached = 0 或 1（即時狀態）
└── kube_pod_container_status_waiting_reason = 0 或 1（即時狀態）

不需要 rate()，直接比較值即可。
這和之前的 mysql_perf_schema_* metrics（counter，需要 rate()）不同。
```

### kubelet 相關參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `maxWaitForUnmountDuration` | 6 min | kubelet 等待 unmount 的最長時間 |
| `node-monitor-grace-period` | 40s | Node controller 標記 NotReady 的等待時間 |
| `pod-eviction-timeout` | 5m | Taint-based eviction 的等待時間 |

從 Node 故障到 Volume 成功 re-attach 的最長等待時間：

```
40s (標記 NotReady) + 5m (eviction) + 6m (force detach) ≈ 12 分鐘
```

### Event Exporter（進階選項）

如果需要最精確的 Multi-Attach error 偵測，可以部署 event exporter 將 Kubernetes Event 轉成 Prometheus metric：

```
工具選項：
├── kubernetes-event-exporter (Grafana Labs) — 支援多後端（ES, Slack, webhook）
└── caicloud/event_exporter — 社群工具，轉成 Prometheus metric

可捕捉的事件：
├── FailedAttachVolume — volume attach 失敗
├── FailedMount — mount 失敗
└── Multi-Attach error for volume "pvc-xxx" — 精確的 multi-attach 事件
```

---

## 相關資源

- [Kubernetes: Storage - Persistent Volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)
- [kube-state-metrics: Exposed Metrics](https://github.com/kubernetes/kube-state-metrics/tree/main/docs)
- [Prometheus: Vector Matching](https://prometheus.io/docs/prometheus/latest/querying/operators/#vector-matching)
