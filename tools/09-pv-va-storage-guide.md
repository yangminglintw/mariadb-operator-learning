# PV / VolumeAttachment 完整學習指南

從基礎概念到監控操作，完整理解 Kubernetes 儲存體系。

---

## 一、Kubernetes 儲存三層模型

### Pod → PVC → PV 的關係

```
Pod                         「我需要一個 volume 來存資料」
 │ spec.volumes[].persistentVolumeClaim.claimName
 ▼
PVC (PersistentVolumeClaim)  「我需要 10Gi RWO 儲存空間」  ← 需求單
 │ spec.volumeName (binding 後填入)
 ▼
PV (PersistentVolume)        「我是 NFS/iSCSI/EBS 上的 10Gi 空間」  ← 實際儲存
```

### 為什麼要分三層

| 層級 | 角色 | 誰管理 |
|------|------|--------|
| Pod | 使用 volume（掛載路徑） | 開發者 |
| PVC | 宣告儲存需求（大小、Access Mode） | 開發者 |
| PV | 對應底層儲存（NFS path、iSCSI target） | 管理員 / StorageClass 自動建立 |

分離的好處：Pod 不需要知道底層儲存細節，只需說「我要 10Gi」。

### StorageClass：自動配對器

```
沒有 StorageClass（Static Provisioning）：
  管理員手動建 PV → PVC 找到符合條件的 PV → Binding

有 StorageClass（Dynamic Provisioning）：
  PVC 指定 storageClassName → StorageClass 自動建 PV → Binding
```

| 項目 | Static Provisioning | Dynamic Provisioning |
|------|--------------------|-----------------------|
| PV 誰建立 | 管理員手動 | StorageClass + Provisioner 自動 |
| 適用場景 | 固定儲存、特殊需求 | 一般用途、雲端環境 |
| 彈性 | 低（需預先建好） | 高（隨需建立） |
| 常見 Provisioner | — | aws-ebs, csi-ceph, netapp-san |

### 類比

```
PVC  = 訂餐單     「我要一份 10 吋的披薩」
PV   = 實際做出的餐 「這是你的 10 吋瑪格麗特披薩」
StorageClass = 餐廳 「我是 Pizza Hut，收到訂單就做」

Static  = 自助餐（已經做好放在那，你去挑一個符合的）
Dynamic = 點餐制（你下單，廚房現做一個給你）
```

---

## 二、VolumeAttachment：PV 掛載到 Node 的橋樑

### VA 是什麼

VolumeAttachment（VA）是一個 Kubernetes API 物件，記錄「某個 PV 掛載到某個 Node」的動作。

```yaml
apiVersion: storage.k8s.io/v1
kind: VolumeAttachment
metadata:
  name: csi-a1b2c3d4e5     # CSI attacher 生成的 hash
spec:
  attacher: csi.example.com  # CSI driver 名稱
  nodeName: worker-2         # 掛載到哪個 Node
  source:
    persistentVolumeName: pvc-xxx-yyy  # 掛載哪個 PV
status:
  attached: true              # 是否已成功掛載
```

### VA 的生命週期

```
1. Pod 被調度到 Node B
     ↓
2. AD controller（kube-controller-manager 內）建立 VolumeAttachment
   spec: { PV: pvc-xxx, Node: worker-2 }
   status: { attached: false }
     ↓
3. CSI driver 執行 ControllerPublishVolume RPC
   （實際把儲存設備連接到 Node，如 iSCSI login）
     ↓
4. CSI driver 回報成功
   status: { attached: true }
     ↓
5. kubelet 執行 mount（把 block device mount 到 pod 的路徑）
     ↓
6. Pod 使用 volume

--- 清理流程 ---

7. Pod 刪除 → kubelet unmount
     ↓
8. AD controller 刪除 VolumeAttachment
     ↓
9. CSI driver 執行 ControllerUnpublishVolume RPC
   （斷開儲存設備，如 iSCSI logout）
```

### VA 名稱的由來

```
VA 名稱 = CSI attacher 根據 PV name + Node name 生成的 hash

同一個 PV 掛到不同 Node → 不同的 hash → 不同的 VA 名稱
  VA csi-aaa = PV pvc-xxx → Node A
  VA csi-bbb = PV pvc-xxx → Node B
```

### 為什麼一個 PV 可以有多個 VA（Multi-Attach）

正常狀態下，RWO 的 PV 只有一個 VA。Multi-Attach 發生在：

```
Node A 故障（kubelet 失聯）
  → VA-1 仍在（attached=true），因為沒有 kubelet 來 unmount
  → StatefulSet controller 在 Node B 建立新 pod
  → AD controller 建立 VA-2（attached=true）
  → 同一個 PV 現在有兩個 VA → Multi-Attach！
```

### CSI Driver vs In-tree Provisioner

```
CSI driver（現代做法）：
  → 使用 VolumeAttachment API
  → 每次掛載都建立 VA 物件
  → kube-state-metrics 可以監控

In-tree provisioner（舊做法，如 local-path）：
  → 不使用 VolumeAttachment API
  → 直接由 kubelet 處理掛載
  → 沒有 VA 物件 → 無法用 VA metrics 監控

判斷方式：
  kubectl --context=kind-mdb get storageclass -o wide
  PROVISIONER 欄位：
    rancher.io/local-path     → in-tree（沒有 VA）
    ebs.csi.aws.com           → CSI（有 VA）
    csi.trident.netapp.io     → CSI（有 VA）
```

> **Kind 環境的限制**：Kind 預設使用 `standard` StorageClass（local-path provisioner），
> 不是 CSI driver，不會建立 VolumeAttachment 物件。
> 本指南的 VA 相關操作和監控適用於**生產環境的遠端儲存**（SAN/iSCSI/EBS/Ceph）。

---

## 三、完整物件鏈：Pod → PVC → PV → VA → Node

### 每一層的 Label/Field 對應

```
Pod
  spec.volumes[].persistentVolumeClaim.claimName ──→ PVC name
  spec.nodeName ──→ Node name（調度後）

PVC
  metadata.name ──→ PVC name
  metadata.namespace ──→ namespace
  spec.volumeName ──→ PV name（binding 後）
  spec.storageClassName ──→ StorageClass name

PV
  metadata.name ──→ PV name（全域唯一，不在 namespace 內）
  spec.claimRef.name ──→ 綁定的 PVC name
  spec.claimRef.namespace ──→ PVC 的 namespace

VolumeAttachment
  spec.source.persistentVolumeName ──→ PV name
  spec.nodeName ──→ Node name
  status.attached ──→ 是否掛載成功
```

### kubectl 逐步追蹤

```bash
# Step 1: Pod → PVC（找出 pod 用了哪個 PVC）
kubectl --context=kind-mdb get pod mariadb-chaos-1 \
  -o jsonpath='{.spec.volumes[*].persistentVolumeClaim.claimName}'
# 輸出：storage-mariadb-chaos-1

# Step 2: PVC → PV（找出 PVC 綁定了哪個 PV）
kubectl --context=kind-mdb get pvc storage-mariadb-chaos-1 \
  -o jsonpath='{.spec.volumeName}'
# 輸出：pvc-abc123-def456

# Step 3: PV → VA（找出這個 PV 有幾個 VolumeAttachment）
kubectl --context=kind-mdb get volumeattachment \
  -o custom-columns='NAME:.metadata.name,PV:.spec.source.persistentVolumeName,NODE:.spec.nodeName,ATTACHED:.status.attached' \
  | grep pvc-abc123-def456
# 正常：一行（一個 VA）
# Multi-Attach：兩行（兩個 VA，掛在不同 Node）

# Step 4: 一次看全部 VA 的完整資訊
kubectl --context=kind-mdb get volumeattachment \
  -o custom-columns='NAME:.metadata.name,PV:.spec.source.persistentVolumeName,NODE:.spec.nodeName,ATTACHED:.status.attached'
```

### kube-state-metrics 中的 Join 鏈

```
Prometheus 指標之間的 label 對應：

kube_pod_spec_volumes_persistentvolumeclaims_info
  labels: pod, namespace, persistentvolumeclaim
  │ join on (namespace, persistentvolumeclaim)
  ▼
kube_persistentvolumeclaim_info
  labels: persistentvolumeclaim, namespace, storageclass, volumename
  │ join on (volumename)
  ▼
kube_volumeattachment_spec_source_persistentvolume  ← Bridge metric!
  labels: volumeattachment, volumename
  │ join on (volumeattachment)
  ▼
kube_volumeattachment_status_attached
  labels: volumeattachment, node
  │ join on (node)
  ▼
kube_node_status_condition
  labels: node, condition, status
```

> **Bridge metric** 是串接 PV 和 VA 的關鍵。
> 完整的 PromQL join chain 和告警規則見 [07-pvc-multiattach-promql.md](./07-pvc-multiattach-promql.md#完整-join：PVC-→-PV-→-VolumeAttachment-→-Node)。

---

## 四、Access Mode 與 Multi-Attach

### 四種 Access Mode

| Mode | 全稱 | 意思 | 多 Node 掛載 |
|------|------|------|-------------|
| RWO | ReadWriteOnce | 單一 Node 讀寫 | 不允許 |
| ROX | ReadOnlyMany | 多 Node 唯讀 | 允許 |
| RWX | ReadWriteMany | 多 Node 讀寫 | 允許 |
| RWOP | ReadWriteOncePod | 單一 Pod 讀寫（K8s 1.27+） | 不允許 |

### 為什麼 RWO 最容易遇到 Multi-Attach

```
RWO 的語義：同一時間只能掛載到一個 Node

正常情況：
  PV → VA (Node A) → OK

Node 故障時：
  PV → VA-1 (Node A, 舊的，kubelet 死了沒 detach)
     → VA-2 (Node B, 新的，StatefulSet 重新調度)
  → 兩個 VA 同時存在 → 違反 RWO → Multi-Attach error

RWX 不會有這個問題：
  PV → VA-1 (Node A) + VA-2 (Node B) → 合法！
  → 但 RWX 通常需要網路檔案系統（NFS、CephFS）
```

### StatefulSet + RWO 的特殊性

```
StatefulSet 的兩個關鍵特性讓 Multi-Attach 更常見：

1. Stable identity（穩定身份）
   Pod 名稱固定：mariadb-0, mariadb-1, mariadb-2
   重建時用同一個名字 → 綁定同一個 PVC → 同一個 PV
   → 不像 Deployment 可以用另一個 PV 避開問題

2. volumeClaimTemplates（自動建 PVC）
   每個 replica 自動建立一個 PVC，且 PVC 不隨 pod 刪除
   → PVC 和 PV 一直存在，等著被同名 pod 重新掛載

結合 RWO：
  mariadb-1 的 PVC 綁定 pvc-xxx（RWO）
  → Node A 故障 → mariadb-1 需要在 Node B 重建
  → 必須掛載同一個 pvc-xxx → Multi-Attach！
```

---

## 五、Volume 掛載流程（時間線）

### 正常流程

```
T+0     Scheduler 把 pod 分配到 Node B
          ↓
T+0     AD controller 建立 VolumeAttachment（status.attached=false）
          ↓
T+1-5s  CSI driver 執行 ControllerPublishVolume
        （連接儲存到 Node，如 iSCSI login、EBS attach）
          ↓
T+5s    VA status.attached=true
          ↓
T+5-10s kubelet 執行 NodeStageVolume + NodePublishVolume
        （format + mount 到 pod 路徑）
          ↓
T+10s   Init containers 開始執行
          ↓
T+15s+  Main containers 啟動
```

### 故障流程：Node NotReady → Multi-Attach

```
T+0     Node A 變成 NotReady（kubelet 失聯）
          → Pod 在 Node A：Terminating（但 kubelet 不回應，pod 不會真的刪）
          → VA-1 仍在 Node A（attached=true）

T+40s   Node controller 標記 pod 為 Terminating
          （node-monitor-grace-period = 40s）

T+5m    Taint-based eviction 生效
          （pod-eviction-timeout = 5m）
          → StatefulSet controller 嘗試在 Node B 建立新 pod

T+5m    AD controller 建立 VA-2（Node B → PV pvc-xxx）
          → VA-1 + VA-2 同時存在 → Multi-Attach！

T+5m    kubelet（Node B）嘗試 attach volume
          → 收到 Multi-Attach error（volume 還在 Node A）
          → Pod 卡在 Pending（Init:0/N 或 ContainerCreating）

T+6m    AD controller 判定 VA-1 需要 force detach
          → 呼叫 CSI driver 的 ControllerUnpublishVolume（對 Node A）
          → 刪除 VA-1

T+6-11m Volume 終於掛載到 Node B，pod 啟動

總等待時間：40s + 5m + 6m ≈ 12 分鐘
```

### Force Detach 機制

```
Force detach 由 AD controller（kube-controller-manager 內）負責：

1. AD controller 偵測到 Node NotReady 超過 grace period
2. 等待 maxWaitForUnmountDuration（預設 6 分鐘）
3. 強制刪除舊 VA（即使 kubelet 沒回應）
4. 呼叫 CSI driver 的 ControllerUnpublishVolume

Force detach 會失敗的情況：
┌─────────────────────────────────────┬──────────────────────────────────────┐
│ 情況                                │ 原因                                  │
├─────────────────────────────────────┼──────────────────────────────────────┤
│ 舊 Node 的 kubelet 還活著但卡住      │ kubelet 持續更新 VA 狀態，              │
│                                     │ AD controller 無法強制 detach           │
├─────────────────────────────────────┼──────────────────────────────────────┤
│ CSI driver 的 Detach RPC 掛住       │ driver 不回應，VA 刪不掉               │
├─────────────────────────────────────┼──────────────────────────────────────┤
│ CSI driver 不支援 force detach      │ 某些 driver 沒實作                     │
├─────────────────────────────────────┼──────────────────────────────────────┤
│ VA 物件孤立（orphaned）              │ 舊 Node 已刪除但 VA 沒被 GC            │
└─────────────────────────────────────┴──────────────────────────────────────┘

最常見的自動恢復：
  Node 完全死掉 → kubelet 停止 → grace period 到期 → force detach 成功
```

### 相關 kubelet / controller 參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `node-monitor-grace-period` | 40s | Node controller 標記 NotReady 的等待時間 |
| `pod-eviction-timeout` | 5m | Taint-based eviction 的等待時間 |
| `maxWaitForUnmountDuration` | 6m | kubelet 等待 unmount 的最長時間 |

---

## 六、監控與偵測

### kube-state-metrics 提供的指標

完整指標表和 PromQL join 語法見 [07-pvc-multiattach-promql.md](./07-pvc-multiattach-promql.md#kube-state-metrics-可用指標)。

核心指標摘要：

| 指標 | 用途 |
|------|------|
| `kube_pod_status_phase{phase="Pending"}` | 偵測卡住的 pod（涵蓋 Init:0/N） |
| `kube_volumeattachment_spec_source_persistentvolume` | Bridge metric：連接 PV 和 VA |
| `kube_volumeattachment_status_attached` | VA 掛載狀態（0/1） |
| `kube_node_status_condition` | Node 健康狀態 |

### 偵測策略：症狀 vs 根因

```
Layer 1 — 症狀偵測（最簡單）：
  「有沒有 pod 卡住超過 5 分鐘？」
  min_over_time(kube_pod_status_phase{phase="Pending"}[5m]) == 1
  → 涵蓋面廣，但不精確（可能是其他原因）

Layer 2 — 根因偵測（最精確，推薦）：
  「有沒有 PV 同時被掛載到多個 Node？」
  count by (volumename) (VA attached) > 1
  → 這就是 Multi-Attach 的定義

Layer 3 — 組合偵測（最完整）：
  「哪個 StatefulSet 的 Pod 因為 Multi-Attach 而卡住？」
  Pending Pod + StatefulSet + PVC → PV → VA count > 1
  → 精確定位受影響的 Pod + PVC + PV
```

### PromQL 查詢參考

所有 PromQL 查詢（基本偵測、完整 join chain、告警規則）見：

- [07-pvc-multiattach-promql.md — 實用 PromQL 查詢](./07-pvc-multiattach-promql.md#實用-PromQL-查詢)
- [07-pvc-multiattach-promql.md — 告警規則範本](./07-pvc-multiattach-promql.md#告警規則範本)

### 正常狀態驗證

```
「查詢回傳 empty 到底是正常還是有問題？」

驗證方式：去掉 > 1 條件，看底層 join 是否正常運作

# 看每個 PV 有幾個 attached VA（正常應該都是 1）
count by (volumename) (
  kube_volumeattachment_spec_source_persistentvolume
    * on(volumeattachment) group_left()
    (kube_volumeattachment_status_attached == 1)
)

結果解讀：
  每個 volumename 的 count = 1  → 正常！查詢邏輯正確，只是沒有 Multi-Attach
  empty                         → join 沒資料（可能環境沒有 VA，如 local-path）
  某個 volumename count = 2     → Multi-Attach 發生中！

搭配 kubectl 確認：
  kubectl --context=kind-mdb get volumeattachment \
    -o custom-columns='PV:.spec.source.persistentVolumeName,NODE:.spec.nodeName,ATTACHED:.status.attached'
```

---

## 七、操作手冊

### 查看 PV/PVC/VA 狀態

```bash
# PVC 列表（含 PV 綁定狀態）
kubectl --context=kind-mdb get pvc

# PV 列表（含回收策略）
kubectl --context=kind-mdb get pv

# PV 詳細資訊（看 claimRef 確認綁定的 PVC）
kubectl --context=kind-mdb get pv <pv-name> -o yaml

# VA 列表（含掛載 Node 和狀態）
kubectl --context=kind-mdb get volumeattachment \
  -o custom-columns='NAME:.metadata.name,ATTACHER:.spec.attacher,PV:.spec.source.persistentVolumeName,NODE:.spec.nodeName,ATTACHED:.status.attached'

# 某個 PV 的所有 VA（Multi-Attach 檢查）
kubectl --context=kind-mdb get volumeattachment \
  -o custom-columns='NAME:.metadata.name,PV:.spec.source.persistentVolumeName,NODE:.spec.nodeName,ATTACHED:.status.attached' \
  | grep <pv-name>
```

### 從 Pod 追蹤到 Node（完整鏈）

```bash
# 一次追蹤完整鏈（用 bash 變數串接）
POD=mariadb-chaos-1
CTX=kind-mdb

PVC=$(kubectl --context=$CTX get pod $POD \
  -o jsonpath='{.spec.volumes[?(@.persistentVolumeClaim)].persistentVolumeClaim.claimName}')
echo "PVC: $PVC"

PV=$(kubectl --context=$CTX get pvc $PVC -o jsonpath='{.spec.volumeName}')
echo "PV: $PV"

echo "VolumeAttachments for PV $PV:"
kubectl --context=$CTX get volumeattachment \
  -o custom-columns='NAME:.metadata.name,PV:.spec.source.persistentVolumeName,NODE:.spec.nodeName,ATTACHED:.status.attached' \
  | grep "$PV"
```

### 手動清理孤立 VA

> **注意**：手動刪除 VA 是危險操作。只在確認 VA 對應的 Node 已不存在或 Pod 已不需要該 volume 時執行。

```bash
# Step 1: 確認 VA 是孤立的（對應的 Node 不存在或 Pod 已刪除）
kubectl --context=kind-mdb get volumeattachment <va-name> -o yaml

# Step 2: 確認 VA 對應的 Node 狀態
kubectl --context=kind-mdb get node <node-name>

# Step 3: 確認沒有 pod 在使用這個 PV
kubectl --context=kind-mdb get pods --all-namespaces \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.volumes[*].persistentVolumeClaim.claimName}{"\n"}{end}' \
  | grep <pvc-name>

# Step 4: 刪除孤立 VA（確認安全後）
kubectl --context=kind-mdb delete volumeattachment <va-name>
```

### 確認 StorageClass 是否使用 CSI

```bash
# 列出所有 StorageClass（看 PROVISIONER 欄位）
kubectl --context=kind-mdb get storageclass -o wide

# CSI driver 才會建立 VA：
#   PROVISIONER = *.csi.* → 有 VA → 可以用 VA metrics 監控
#   PROVISIONER = rancher.io/local-path → 沒有 VA

# 確認 CSI driver 支援的功能
kubectl --context=kind-mdb get csidrivers -o wide
```

### 確認 CSI Driver 是否支援 Force Detach

```bash
# 查看 CSI driver 的 spec（看 attachRequired 和相關設定）
kubectl --context=kind-mdb get csidriver <driver-name> -o yaml

# 關鍵欄位：
#   spec.attachRequired: true   → 使用 VA（需要 attach/detach）
#   spec.attachRequired: false  → 不使用 VA（如 NFS CSI）
```

### 常見故障排查流程

```
Pod 卡在 Pending / Init:0/N → 懷疑 Multi-Attach：

1. kubectl --context=kind-mdb describe pod <pod-name>
   → 看 Events 有沒有「Multi-Attach error for volume」

2. 追蹤 PV
   → 用上方「從 Pod 追蹤到 Node」的指令

3. 檢查 VA 數量
   → 同一個 PV 有兩個 VA → 確認 Multi-Attach

4. 判斷是否會自動恢復
   → 舊 Node 完全死掉 → 等 6 分鐘 force detach
   → 舊 Node kubelet 還活著 → 需要手動處理

5. 手動處理（如果不會自動恢復）
   → 確認安全後刪除舊 VA
   → 或 drain/delete 舊 Node
```

---

## 相關文件

- [07-pvc-multiattach-promql.md](./07-pvc-multiattach-promql.md) — PromQL 監控查詢、告警規則、實際案例
- [Kubernetes: Persistent Volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)
- [Kubernetes: VolumeAttachment](https://kubernetes.io/docs/reference/kubernetes-api/config-and-storage-resources/volume-attachment-v1/)
- [Kubernetes: Storage Classes](https://kubernetes.io/docs/concepts/storage/storage-classes/)
- [Kubernetes: CSI](https://kubernetes.io/docs/concepts/storage/volumes/#csi)
