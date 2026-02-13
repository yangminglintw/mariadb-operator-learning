# PVC Multi-Attach Error PromQL 監控

## 問題背景

### 什麼是 Multi-Attach Error

```
情境：MariaDB StatefulSet (3 replicas) 跑在 Kubernetes 上

Node A (NotReady) ── mariadb-chaos-1 pod (Terminating，但 kubelet 沒回應)
                              │
                              ▼ RWO PVC（還掛在 Node A）
                              │
Node B ── mariadb-chaos-1 pod (Pending，等 volume detach)
                              └── Multi-Attach error！

結果：新 pod 卡在 Pending（Init:0/N 或 ContainerCreating），直到舊 node 上的 volume 被強制 detach
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
T+5m    新 pod 進入 Pending（Init:0/N 或 ContainerCreating）
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
| `kube_pod_container_status_waiting_reason` | pod, namespace, container, reason | 偵測 ContainerCreating（⚠ 不涵蓋 init container 卡住的情況） |
| `kube_pod_status_phase` | pod, namespace, phase | Pod 狀態（Pending/Running） |
| `kube_pod_spec_volumes_persistentvolumeclaims_info` | pod, namespace, persistentvolumeclaim | Pod → PVC 對應 |
| `kube_persistentvolumeclaim_info` | persistentvolumeclaim, namespace, storageclass, volumename | PVC → PV 對應（⚠ binding 時可能有 stale series） |
| `kube_persistentvolumeclaim_status_phase` | persistentvolumeclaim, namespace, phase | PVC 狀態（Bound/Pending） |
| `kube_volumeattachment_spec_source_persistentvolume` | volumeattachment, volumename | **Bridge metric**：PV → VA |
| `kube_volumeattachment_status_attached` | volumeattachment, node | VA 掛載狀態（0/1） |
| `kube_volumeattachment_info` | volumeattachment, attacher, node | VA 基本資訊 |

### 指標值說明

```
kube_node_status_condition 每個 condition 會產生三個 series：

                          status="true"   status="false"   status="unknown"
Node Ready（正常）            1                0                0
Node NotReady（明確故障）      0                1                0
Node Unknown（kubelet 失聯）   0                0                1

偵測 NotReady 的正確寫法（正向斷言）：
  kube_node_status_condition{condition="Ready", status="false"} == 1
    or
  kube_node_status_condition{condition="Ready", status="unknown"} == 1

⚠ 避免：{status="true"} == 0
  → series 消失時回傳 empty，不是 0
```

```
kube_volumeattachment_status_attached == 1   → Volume 已掛載
kube_volumeattachment_status_attached == 0   → Volume 未掛載

kube_pod_status_phase{phase="Pending"} == 1  → Pod 卡住（涵蓋 Init:0/N 和 ContainerCreating）

⚠ 為什麼不用 kube_pod_container_status_waiting_reason{reason="ContainerCreating"}：
  有 init container 的 pod（如 MariaDB Operator），Multi-Attach 時卡在 Init:0/N，
  此時 regular container 根本還沒開始 → 該 metric 沒有資料。
  kube_pod_status_phase{phase="Pending"} 涵蓋所有情況，更可靠。
```

### Init Container 與 Multi-Attach

MariaDB Operator 注入 init containers（如 init、galera-init）。
Multi-Attach 時的實際表現：

```
kubectl get pods:
  mariadb-chaos-1   0/1   Init:0/2   0   5m    ← 不是 ContainerCreating！

原因：Volume mount 在 pod 層級進行，init container 先啟動。
Volume 掛載失敗 → init container 卡住 → 顯示 Init:0/N
此時 pod phase = Pending → 用 kube_pod_status_phase{phase="Pending"} 偵測最可靠。
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

**新的偵測路徑（不依賴 Node 狀態 — 推薦）：**

```
kube_volumeattachment_spec_source_persistentvolume
  │ join on (volumeattachment)
  ▼
kube_volumeattachment_status_attached == 1
  │ count by (volumename) > 1
  ▼
Multi-Attach 確認！  ← 不需要 Node 狀態，直接從 VA 計數判斷
```

### 另一個解法：label_replace 重新命名 Label

除了 bridge metric，也可以用 `label_replace` 把 label 重新命名以便 join。

```
語法：
  label_replace(向量, "新label名", "$1", "來源label名", "(.*)")

實例：把 volumename 複製為 persistentvolume
  label_replace(
    kube_volumeattachment_spec_source_persistentvolume,
    "persistentvolume", "$1", "volumename", "(.*)"
  )

和 Bridge Metric 的差異：
  Bridge Metric → 靠指標本身同時擁有兩邊的 label
  label_replace  → 靠重新命名 label 讓兩邊可以 join
```

---

## 實用 PromQL 查詢

### 基本偵測

**Node NotReady：**

```promql
# 哪些 node 不健康（正向斷言：偵測 NotReady 或 Unknown）
kube_node_status_condition{condition="Ready", status="false"} == 1
  or
kube_node_status_condition{condition="Ready", status="unknown"} == 1
```

**Pod 卡住超過 5 分鐘：**

```promql
# 偵測 pod 持續 Pending 超過 5 分鐘（涵蓋 Init:0/N 和 ContainerCreating）
min_over_time(kube_pod_status_phase{phase="Pending"}[5m]) == 1
```

**StatefulSet 的 Pod 在 NotReady node 上：**

```promql
# StatefulSet pod 跑在 NotReady node 上（風險前兆）
(kube_pod_info{created_by_kind="StatefulSet"})
  * on(node) group_left()
  (
    kube_node_status_condition{condition="Ready", status="false"} == 1
      or
    kube_node_status_condition{condition="Ready", status="unknown"} == 1
  )
```

> ⚠ 此查詢依賴 `kube_node_status_condition`，可能不可靠：
> - kubelet 失聯時 series 可能消失或 stale
> - Node 短暫 flapping 造成誤報
> - 未涵蓋所有 Multi-Attach 情境（如 force drain 時 Node 可能是 Ready）
>
> 更可靠的替代：直接偵測 VA count > 1，見下方「Step 4」及方案 A 告警。

### 完整 Join：PVC → PV → VolumeAttachment → Node

**Step 1：用 bridge metric 串接 PVC 到 VolumeAttachment**

```promql
# PVC → PV（透過 kube_persistentvolumeclaim_info）
# PV → VA（透過 bridge metric）
# VA → attached + node
(kube_volumeattachment_status_attached == 1)
  * on(volumeattachment) group_left(volumename)
  kube_volumeattachment_spec_source_persistentvolume
```

**Step 2：加上 PVC 資訊**

```promql
# 完整鏈：VA attached status + PV name + PVC name
(
  (kube_volumeattachment_status_attached == 1)
    * on(volumeattachment) group_left(volumename)
    kube_volumeattachment_spec_source_persistentvolume
)
  * on(volumename) group_left(persistentvolumeclaim, namespace)
  kube_persistentvolumeclaim_info
```

> ⚠ 在告警規則中使用 `kube_persistentvolumeclaim_info` 做 `group_left` join 時，
> 應用 `max by()` 去重，避免 PVC binding 過程中的 stale series 造成 many-to-many。
> 見方案 A 告警 3 的寫法。

**Step 3：Volume 掛在 NotReady node 上（Multi-Attach 風險偵測）**

```promql
# 核心告警查詢：volume 還 attached 在一個 NotReady 的 node 上
(
  (kube_volumeattachment_status_attached == 1)
    * on(volumeattachment) group_left(volumename)
    kube_volumeattachment_spec_source_persistentvolume
)
  * on(node) group_left()
  (
    kube_node_status_condition{condition="Ready", status="false"} == 1
      or
    kube_node_status_condition{condition="Ready", status="unknown"} == 1
  )
```

**Step 4：直接 Multi-Attach 偵測（推薦 — 不依賴 Node 狀態）**

```promql
# 直接 Multi-Attach 偵測：每個 PV 的 active VA 數量 > 1
# 這就是 Multi-Attach 的定義 — 不需要繞道 Node 狀態
count by (volumename) (
  kube_volumeattachment_spec_source_persistentvolume
    * on(volumeattachment) group_left()
    (kube_volumeattachment_status_attached == 1)
) > 1
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

### 方案 A：with VolumeAttachment（VA metrics 可靠的環境）

適用於 kube-state-metrics 有啟用 `kube_volumeattachment_*` 且在 Node 故障期間 metrics 不會消失的環境。

```yaml
groups:
- name: pvc-multiattach-alerts-plan-a
  rules:

  # ═══════════════════════════════════════════════
  # 告警 1：症狀偵測（最實用，不需要複雜 join）
  # ═══════════════════════════════════════════════
  - alert: PodStuckPending
    expr: |
      min_over_time(kube_pod_status_phase{phase="Pending"}[5m]) == 1
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "Pod stuck in Pending"
      description: |
        Pod {{ $labels.namespace }}/{{ $labels.pod }} has been Pending
        for more than 5 minutes.
        Possible causes: Multi-Attach error (Init:0/N), image pull failure,
        resource quota, node affinity.

  # ═══════════════════════════════════════════════
  # 告警 2：直接 Multi-Attach 偵測（VA count > 1）
  # 不依賴 kube_node_status_condition，更可靠
  # ═══════════════════════════════════════════════
  - alert: PVMultipleVolumeAttachments
    expr: |
      count by (volumename) (
        kube_volumeattachment_spec_source_persistentvolume
          * on(volumeattachment) group_left()
          (kube_volumeattachment_status_attached == 1)
      ) > 1
    for: 1m
    labels:
      severity: critical
    annotations:
      summary: "PV has multiple active VolumeAttachments (Multi-Attach)"
      description: |
        PV {{ $labels.volumename }} has {{ $value }} active VolumeAttachments.
        This IS a Multi-Attach situation — a pod using this RWO volume on
        another node will be stuck.
        Check: kubectl get volumeattachment -o wide | grep {{ $labels.volumename }}

  # ═══════════════════════════════════════════════
  # 告警 3：組合偵測 — Pending Pod + PVC 的 PV 有多個 VA
  # 結合症狀（Pod Pending）和根因（Multi-Attach）
  # ═══════════════════════════════════════════════
  - alert: PendingPodWithMultiAttachedPVC
    # ⚠ kube_pod_info 和 kube_persistentvolumeclaim_info 都用 max by() 去重
    # 避免 pod 重建或 PVC binding 時 stale series 導致 many-to-many
    expr: |
      (
        kube_pod_spec_volumes_persistentvolumeclaims_info
          * on(namespace, pod) group_left(phase)
          (kube_pod_status_phase{phase="Pending"} == 1)
          * on(namespace, pod) group_left(created_by_name)
          (
            max by (namespace, pod, created_by_name) (
              kube_pod_info{created_by_kind="StatefulSet"}
            )
          )
          * on(namespace, persistentvolumeclaim) group_left(volumename)
          (
            max by (namespace, persistentvolumeclaim, volumename) (
              kube_persistentvolumeclaim_info{volumename!=""}
            )
          )
      )
        and on(volumename)
        (
          count by (volumename) (
            kube_volumeattachment_spec_source_persistentvolume
              * on(volumeattachment) group_left()
              (kube_volumeattachment_status_attached == 1)
          ) > 1
        )
    for: 2m
    labels:
      severity: critical
    annotations:
      summary: "Pending StatefulSet pod's PVC has Multi-Attach"
      description: |
        Pod {{ $labels.namespace }}/{{ $labels.pod }}
        (StatefulSet: {{ $labels.created_by_name }})
        is Pending and its PVC {{ $labels.persistentvolumeclaim }}
        (PV: {{ $labels.volumename }}) has multiple active VolumeAttachments.
        Check: kubectl describe pod {{ $labels.pod }} -n {{ $labels.namespace }}
```

**告警 3 Join 鏈解說：**

```
kube_pod_spec_volumes_persistentvolumeclaims_info  ← 起點（每 pod-PVC 一筆）
  │ * on(namespace, pod) → kube_pod_status_phase{Pending}
  │ * on(namespace, pod) → kube_pod_info{StatefulSet}
  │ * on(namespace, persistentvolumeclaim) → max by() kube_persistentvolumeclaim_info  → 得到 volumename
  ▼
and on(volumename) → count VA per volumename > 1  ← Multi-Attach 確認
```

### 環境特定調整（選用）

#### StorageClass 過濾

只監控特定 StorageClass（如 NetApp SAN）：

```promql
# 在告警 3 中替換 kube_persistentvolumeclaim_info 加上篩選
* on(namespace, persistentvolumeclaim) group_left(volumename)
kube_persistentvolumeclaim_info{storageclass=~"netapp-san.*"}
```

#### iSCSI Metadata（CSI driver 特定）

⚠ `kube_volumeattachment_status_attachment_metadata` 不是標準 kube-state-metrics 指標。
是否存在取決於 CSI driver 和 kube-state-metrics 配置。

#### Pod 建立時間

加入 `kube_pod_created * 1000` 可在 Grafana 中顯示 pod 卡住的起始時間。

### 方案 B：without VolumeAttachment（VA metrics 不可靠的環境）

不依賴 `kube_volumeattachment_*` 指標，只用 Pod、PVC、Node 層級的 metrics。
適用於 VA metrics 在 Node 故障時消失或 kube-state-metrics 版本不支援 VA 的環境。

```yaml
groups:
- name: pvc-multiattach-alerts-plan-b
  rules:

  # ═══════════════════════════════════════════════
  # 告警 1：症狀偵測（同方案 A）
  # ═══════════════════════════════════════════════
  - alert: PodStuckPending
    expr: |
      min_over_time(kube_pod_status_phase{phase="Pending"}[5m]) == 1
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "Pod stuck in Pending"
      description: |
        Pod {{ $labels.namespace }}/{{ $labels.pod }} has been Pending
        for more than 5 minutes.
        Possible causes: Multi-Attach error (Init:0/N), image pull failure,
        resource quota, node affinity.

  # ═══════════════════════════════════════════════
  # 告警 2：前兆偵測（次優方案 — VA metrics 不可用時的替代）
  # ⚠ kube_node_status_condition 的局限性見「限制與注意事項」段落
  # ═══════════════════════════════════════════════
  - alert: NodeNotReadyWithStatefulSet
    expr: |
      count by (node) (
        (kube_pod_info{created_by_kind="StatefulSet"})
          * on(node) group_left()
          (
            kube_node_status_condition{condition="Ready", status="false"} == 1
              or
            kube_node_status_condition{condition="Ready", status="unknown"} == 1
          )
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
  # 告警 3：StatefulSet Pod 使用 PVC 且卡在 Pending
  # 不依賴 VolumeAttachment metrics
  # ═══════════════════════════════════════════════
  - alert: StatefulSetPodPendingWithPVC
    # ⚠ kube_pod_info 用 max by() 去重，避免 pod 重建時 stale series 導致 many-to-many
    expr: |
      (
        (min_over_time(kube_pod_status_phase{phase="Pending"}[5m]) == 1)
          * on(namespace, pod) group_left(created_by_name)
          (
            max by (namespace, pod, created_by_name) (
              kube_pod_info{created_by_kind="StatefulSet"}
            )
          )
      )
        and on(namespace, pod)
        kube_pod_spec_volumes_persistentvolumeclaims_info
    for: 5m
    labels:
      severity: critical
    annotations:
      summary: "StatefulSet pod with PVC stuck in Pending"
      description: |
        Pod {{ $labels.namespace }}/{{ $labels.pod }} (StatefulSet: {{ $labels.created_by_name }})
        has been Pending for more than 5 minutes and uses a PVC.
        This is likely a Multi-Attach error caused by a node failure.
        Check: kubectl describe pod {{ $labels.pod }} -n {{ $labels.namespace }}
```

備註：如果環境有特定 StorageClass，可加篩選：
```yaml
      # 只監控 NetApp SAN（範例）
      and on(namespace, pod)
      (
        kube_pod_spec_volumes_persistentvolumeclaims_info
          * on(namespace, persistentvolumeclaim) group_left()
          kube_persistentvolumeclaim_info{storageclass=~"netapp-san.*"}
      )
```

---

## 實際案例與驗證

### 案例一：Node NotReady → Multi-Attach（最常見情境）

```
時間線：

T+0     Node A 變成 NotReady（kubelet 失聯）
          Pod mariadb-chaos-1 在 Node A：Terminating
          VA-1 仍在（attached=true）—— kubelet 死了，沒人能 detach
                                                                ┌──────────┐
T+40s   Node controller 標記 pod 為 Terminating                 │ VA 數量   │
          （node-monitor-grace-period = 40s）                    │          │
                                                                │   1 個    │
T+5m    StatefulSet controller 在 Node B 建立新 pod             │          │
          AD controller 建立 VA-2（Node B, attached=true）       │   2 個 ← │ Multi-Attach!
                                                                │          │
T+5m    新 pod 卡在 Pending（Init:0/N）                         │   2 個    │
          kubectl describe pod 可看到：                          │          │
          "Multi-Attach error for volume pvc-xxx"               │          │
                                                                │          │
T+11m   AD controller force detach VA-1（刪除舊 VA）            │   1 個    │
          Volume 掛載到 Node B，pod 正常啟動                     └──────────┘
```

**自動恢復條件**：Node 完全死掉 → kubelet 停止更新 → AD controller 能成功 force detach。

**不會自動恢復的情況**：

| 情況 | 原因 | 處理 |
|------|------|------|
| 舊 Node 的 kubelet 還活著但卡住 | kubelet 持續更新 VA 狀態，AD controller 無法 force detach | drain 或 delete Node |
| CSI driver 的 Detach RPC 掛住 | driver 不回應，VA 刪不掉 | 排查 CSI driver |
| VA 物件孤立（orphaned） | 舊 Node 已刪除但 VA 沒被 GC | 手動刪除 VA |

### 案例二：Multi-Attach + ImagePullBackOff（兩個獨立問題）

```
觀察到的現象：
  kubectl get pods:
    mariadb-chaos-1   0/1   Init:ImagePullBackOff   0   8m

  kubectl describe pod：同時出現兩種 event：
    1. "Multi-Attach error for volume pvc-xxx"  ← volume 問題
    2. "Failed to pull image xxx"               ← image 問題
```

**關鍵理解**：這是兩個**獨立問題**碰巧同時發生，不是因果關係。

```
時間線：

T+0     Node A 故障 → pod 遷移到 Node B

T+5m    AD controller 建立 VA-2（Node B）
          → VA-1（Node A）還沒清掉
          → 兩個 VA 同時存在 → Multi-Attach     ← 問題 1

T+5m    kubelet（Node B）嘗試啟動 init container
          → 拉 image 失敗 → ImagePullBackOff     ← 問題 2（獨立）

T+11m   Force detach 成功，VA-1 刪除
          → Multi-Attach 解除                     ← 問題 1 解決

T+11m+  但 pod 仍卡在 ImagePullBackOff            ← 問題 2 未解決
          → 需要另外處理 image 問題
```

**為什麼 VA 在 image pull 之前就建立了？**

```
Volume 掛載流程先於 container 啟動：
  schedule → attach (VA 建立) → mount → init containers → main containers
                                              ↑
                                         image pull 在這裡
所以即使 image 有問題，VA 已經建立了。
```

### 查詢驗證方法

在實際環境中驗證 PromQL 查詢是否正常運作：

**方法 1：去掉 `> 1` 看底層 join**

```promql
# 正常時每個 PV 的 VA count 應為 1
count by (volumename) (
  kube_volumeattachment_spec_source_persistentvolume
    * on(volumeattachment) group_left()
    (kube_volumeattachment_status_attached == 1)
)

# 結果解讀：
#   {volumename="pvc-xxx"} 1   → 正常（1 個 VA）
#   {volumename="pvc-yyy"} 1   → 正常
#   empty                       → 環境沒有 VA（如 local-path provisioner）
```

**方法 2：kubectl 交叉確認**

```bash
# 看所有 VA 的 PV 和 Node 對應
kubectl --context=kind-mdb get volumeattachment \
  -o custom-columns='NAME:.metadata.name,PV:.spec.source.persistentVolumeName,NODE:.spec.nodeName,ATTACHED:.status.attached'

# 結果應和 PromQL 的 count by (volumename) 一致
```

**方法 3：確認環境前置條件**

```bash
# 確認 StorageClass 是否使用 CSI（只有 CSI 才有 VA）
kubectl --context=kind-mdb get storageclass -o wide
# PROVISIONER 是 *.csi.* → 有 VA
# PROVISIONER 是 rancher.io/local-path → 沒有 VA（查詢必定 empty）

# 確認 kube-state-metrics 有啟用 VA metrics
# 在 Prometheus UI 查詢：kube_volumeattachment_spec_source_persistentvolume
# 有結果 → 可以用完整 join chain
```

**Kind 環境（local-path）的預期行為**：

```
Kind 使用 standard StorageClass（local-path provisioner），不是 CSI driver：
  → 不會建立 VolumeAttachment 物件
  → 所有 VA 相關的 PromQL 查詢回傳 empty
  → 這是正常行為，不是查詢有問題

查詢適用於生產環境的遠端儲存（SAN/iSCSI/EBS/Ceph）。
```

---

## 監控策略比較

| 方法 | 可行性 | 精確度 | 前置需求 | 適用場景 |
|------|--------|--------|----------|----------|
| 症狀偵測（Pending > 5min） | 高 | 中 | kube-state-metrics | 通用告警，涵蓋 Init:0/N、ContainerCreating 等 |
| 前兆偵測（NotReady + StatefulSet） | 高 | 中 | kube-state-metrics | 提前警告，但可能誤報（見限制說明） |
| 直接 VA 計數（VA count > 1）— 方案 A | 高 | 最高 | VA metrics 可靠 | 直接偵測 Multi-Attach，不依賴 Node 狀態 |
| StatefulSet + PVC 偵測 — 方案 B | 高 | 中高 | 僅 Pod/PVC metrics | VA metrics 不可靠時的替代 |
| Event exporter | 最精確 | 高 | 需額外部署 event-exporter | 捕捉 `FailedAttachVolume` 事件 |

### 方案選擇指南

```
先確認你的環境：
  kubectl get --raw /metrics | grep kube_volumeattachment_status_attached
  → 有結果且 Node 故障時不消失 → 用方案 A
  → 沒有結果或 Node 故障時消失 → 用方案 B
```

### 建議組合

```
方案 A（VA metrics 可靠）— 推薦：
  Layer 1（症狀）：PodStuckPending
    └── 最簡單，覆蓋面廣，5 分鐘內告警
  Layer 2（根因）：PVMultipleVolumeAttachments
    └── 直接偵測 Multi-Attach，不依賴 Node 狀態
  Layer 3（定位）：PendingPodWithMultiAttachedPVC
    └── 精確定位受影響的 Pod + PVC + PV

方案 B（VA metrics 不可靠）：
  Layer 1（症狀）：PodStuckPending
    └── 同上
  Layer 2（前兆）：NodeNotReadyWithStatefulSet
    └── 次優方案，但無 VA metrics 時是唯一前兆偵測手段
  Layer 3（關聯）：StatefulSetPodPendingWithPVC
    └── 不依賴 VA，用 Pod + PVC + StatefulSet 關聯偵測
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
└── kube_pod_status_phase = 0 或 1（即時狀態）

不需要 rate()，直接比較值即可。
這和之前的 mysql_perf_schema_* metrics（counter，需要 rate()）不同。
```

### Prometheus 查詢的 empty 陷阱

```
問題：為什麼 {status="true"} == 0 在 Node 故障時回傳 empty？

Prometheus 的比較運算子（==, !=, >, <）只對「存在的 series」運算。
如果一個 series 完全不存在（或已 stale），== 0 不會回傳 0，而是什麼都不回傳。
```

**Node condition 的三態模型：**

```
kube_node_status_condition{condition="Ready"} 有三個 series：
  status="true"     → 正常時 = 1，故障時 = 0（或 series 消失）
  status="false"    → NotReady 時 = 1
  status="unknown"  → kubelet 失聯時 = 1

kubelet 失聯時的狀態是 Unknown，不是 False！
  → 如果只查 status="false"，會漏掉 kubelet 失聯的情況
  → 正確做法：同時查 status="false" 和 status="unknown"
```

**VolumeAttachment metrics 的消失問題：**

```
Node 故障時，kube-state-metrics 可能無法 scrape 到 VolumeAttachment metrics：
  → kube_volumeattachment_status_attached series 變成 stale 後被 Prometheus 移除
  → 任何依賴 VA metrics 的 join chain 都會回傳 empty

這就是為什麼需要方案 B（不依賴 VA metrics）作為備案。
```

**經驗法則：**

```
✗ 反向斷言（fragile）：metric{status="true"} == 0
  → series 消失時回傳 empty

✓ 正向斷言（robust）：metric{status="false"} == 1
  → 只要故障 series 存在就會觸發

永遠用正向斷言偵測異常狀態。
```

### 為什麼 kube_node_status_condition 不適合作為主要偵測手段

```
1. Node NotReady ≠ Multi-Attach（語義間接）
   → Node 可以因為很多原因 NotReady，不一定有 volume 問題
   → Multi-Attach 也可能發生在 Node 仍然 Ready 的情況（如 force drain）

2. kubelet 失聯時 series 可能消失或 stale
   → kube_node_status_condition 依賴 kube-state-metrics 能 scrape 到 Node 狀態
   → 某些故障情境下 series 可能短暫 flapping 造成誤報

3. force drain 時 Node 可能仍 Ready，但 VA 未正確清理
   → 此時 kube_node_status_condition 不會觸發，但 Multi-Attach 已經發生

4. VA count > 1 per PV 才是 Multi-Attach 的定義
   → count by (volumename) (VA attached) > 1 直接表達語義
   → 如果 VA metrics 可用，應優先使用直接偵測（方案 A）
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
