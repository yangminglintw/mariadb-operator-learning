# MariaDB Service 與 Endpoint 監控指南

## 一、mariadb-operator 建立的 Services

mariadb-operator 為每個 MariaDB 叢集建立 **5 個 Service**，各有不同用途：

| Service | 類型 | 用途 | Endpoint 數量 |
|---------|------|------|-------------|
| `<name>` | ClusterIP | 所有 pods（Primary + Replicas） | 全部（3） |
| `<name>-primary` | ClusterIP | **僅 Primary pod** | 1 |
| `<name>-secondary` | ClusterIP | **僅 Replica pods**（排除 Primary） | N-1（2） |
| `<name>-internal` | Headless (None) | 內部通訊（agent sidecar） | 全部（3） |
| `<name>-metrics` | ClusterIP | mysqld-exporter metrics | 1 |

### Service → Pod 對應圖

```
                    ┌─────────────┐
    App 寫入 ──────►│   -primary  │──► Pod-1 (Primary)
                    └─────────────┘
                                        ▲
                    ┌─────────────┐      │
    App 讀取 ──────►│ -secondary  │──► Pod-0 (Replica)
                    └─────────────┘  └► Pod-2 (Replica)

                    ┌─────────────┐
    App 讀寫 ──────►│   <name>    │──► Pod-0, Pod-1, Pod-2（全部）
                    └─────────────┘

                    ┌─────────────┐
    Agent 通訊 ────►│  -internal  │──► Pod-0, Pod-1, Pod-2（headless）
                    └─────────────┘
```

### Primary Service 的動態 Selector

Primary service 用 **pod 名稱** 作為 selector：

```yaml
# kubectl get svc <name>-primary -o yaml
spec:
  selector:
    app.kubernetes.io/instance: mariadb-chaos
    app.kubernetes.io/name: mariadb
    statefulset.kubernetes.io/pod-name: mariadb-chaos-1  # ← 指向當前 Primary
```

Switchover/Failover 時，operator 更新此 selector 指向新 Primary pod。

### Secondary Service 的特殊設計

Secondary service **沒有 selector**：

```yaml
# kubectl get svc <name>-secondary -o yaml
spec:
  selector: null  # ← 沒有 selector！
```

Operator 直接管理 EndpointSlice（不透過 Kubernetes selector 自動生成）。
這意味著：
- **kube-state-metrics 可能不追蹤此 service 的 endpoint**
- 需要用 EndpointSlice API 或 operator 自身的 metrics 監控

---

## 二、Endpoint Alert 設計

### MariaDBPrimaryEndpointDown

偵測 Primary service 沒有可用 endpoint（Primary pod 故障）。

#### kube-state-metrics < v2.14（舊版）

```yaml
- alert: MariaDBPrimaryEndpointDown
  expr: |
    kube_endpoint_address_available{endpoint=~".*-primary"} == 0
  for: 1m
  labels:
    severity: critical
  annotations:
    summary: "MariaDB Primary endpoint 不可用"
    description: |
      Service {{ $labels.endpoint }} 沒有可用的 endpoint。
      Primary pod 可能故障，App 的寫入流量無法送達。
      檢查：kubectl get endpoints {{ $labels.endpoint }} -n {{ $labels.namespace }}
```

#### kube-state-metrics >= v2.14（新版）

`kube_endpoint_address_available` 在 v2.14（2024-11）被移除，替代方式：

```yaml
- alert: MariaDBPrimaryEndpointDown
  expr: |
    kube_endpoint_address{endpoint=~".*-primary"} == 0
    or
    absent(kube_endpoint_address{endpoint=~".*-primary"})
  for: 1m
  labels:
    severity: critical
  annotations:
    summary: "MariaDB Primary endpoint 不可用"
    description: |
      Service {{ $labels.endpoint }} 沒有可用的 endpoint。
```

#### 你們內部系統的 regex 調整

```yaml
# Community 版 service 命名：
#   <name>-primary  → regex: .*-primary
#
# 你們內部版命名（確認你的 service 名稱）：
#   <name>-mariadb-repl-primary → regex: .*-mariadb-repl-primar.*
#
# 根據你的環境調整 regex：
expr: kube_endpoint_address_available{endpoint=~".*-mariadb-repl-primar.*"} == 0
```

### 為什麼加 `for: 1m`？

```
正常 Switchover 時序：

時間 ──────────────────────────────────────────►

Primary Service Endpoint:
  Pod-1 ──── selector 更新中 ──── Pod-0
              ◄── 短暫 0 endpoint ──►
              （幾秒到幾十秒）

如果 for: 0m → Switchover 也會觸發 alert（誤報）
如果 for: 1m → 正常 Switchover 不觸發，真正故障才觸發 ✓
```

---

## 三、多層監控建議

單獨監控 endpoint 不夠全面。建議三層監控互補：

### 層級 1：Service Endpoint（service 層）

偵測「App 能不能連到 DB」。

```yaml
# Primary 不可用
- alert: MariaDBPrimaryEndpointDown
  expr: kube_endpoint_address_available{endpoint=~".*-primary"} == 0
  for: 1m
  labels:
    severity: critical

# 全部 endpoint 都不可用（整個叢集掛了）
- alert: MariaDBAllEndpointsDown
  expr: kube_endpoint_address_available{endpoint=~"<name>"} == 0
  for: 1m
  labels:
    severity: critical
```

### 層級 2：Pod Ready（pod 層）

偵測「DB pod 健不健康」。比 endpoint 更早反映問題。

```yaml
# Primary pod NotReady（可能觸發 failover）
- alert: MariaDBPrimaryPodNotReady
  expr: |
    kube_pod_status_ready{
      pod=~"mariadb-chaos-.*",
      condition="true"
    } == 0
  for: 30s
  labels:
    severity: warning

# 多個 pod 同時 NotReady
- alert: MariaDBMultiplePodsNotReady
  expr: |
    count(
      kube_pod_status_ready{
        pod=~"mariadb-chaos-.*",
        condition="true"
      } == 0
    ) >= 2
  for: 1m
  labels:
    severity: critical
```

### 層級 3：MariaDB CR Conditions（operator 層）

偵測「operator 認為 DB 狀態如何」。需要 operator 輸出 metrics 或用 kube-state-metrics 的 custom resource 支援。

```bash
# 手動檢查（kubectl）
kubectl --context=kind-mdb -n default get mdb mariadb-chaos \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.message}{"\n"}{end}'

# 預期輸出：
# Ready=True
# PrimarySwitched=True
```

如果 operator 有輸出 metrics（如 `mariadb_operator_status`），可以建 alert：

```yaml
# 僅在 operator 輸出 metrics 時可用
- alert: MariaDBCRNotReady
  expr: |
    mariadb_operator_mariadb_condition{
      condition="Ready",
      status="True"
    } == 0
  for: 2m
  labels:
    severity: critical
```

### 三層對比

| 層級 | 偵測什麼 | 速度 | 準確度 | 前提 |
|------|---------|------|--------|------|
| 1: Endpoint | App 能不能連到 | 中 | 高 | kube-state-metrics |
| 2: Pod Ready | Pod 健不健康 | **快** | 中（pod NotReady 不一定影響服務） | kube-state-metrics |
| 3: CR Condition | Operator 判斷 | 慢 | **最高**（operator 知道全貌） | Operator 輸出 metrics |

**建議**：至少用層級 1 + 2。層級 3 視 operator 是否輸出 metrics 決定。

---

## 四、Switchover/Failover 期間的監控盲區

### 正常 Switchover 時序

```
時間 ─────────────────────────────────────────────────────►

1. Operator 收到 switchover 請求
   │
2. 鎖定舊 Primary（SET read_only=ON）
   │  ← 此時 endpoint 還在，但寫入會失敗
   │
3. 等待 Replica 同步
   │
4. 更新 Primary Service selector → 指向新 Primary
   │  ← 短暫 0 endpoint（舊的移除、新的還沒生效）
   │  ← 持續幾秒到幾十秒
   │
5. 新 Primary 就緒
   │  ← endpoint 恢復為 1
   │
6. 更新 Secondary Service EndpointSlice
   │
7. Switchover 完成
```

### 如何區分正常 Switchover vs 異常故障

| 線索 | 正常 Switchover | 異常故障 |
|------|----------------|---------|
| Endpoint 為 0 的持續時間 | < 1 分鐘 | > 1 分鐘 |
| Operator logs | 有 switchover 記錄 | 有 error / failover 記錄 |
| CR Condition | PrimarySwitched 正在變化 | Ready=False |
| 觸發原因 | 有人修改了 `spec.replication.primary.podIndex` | 無人操作，自動觸發 |

**Alert 建議**：

```yaml
# 用 for: 1m 過濾正常 switchover
# 如果超過 1 分鐘 Primary endpoint 仍為 0 → 真的有問題
- alert: MariaDBPrimaryEndpointDown
  for: 1m  # ← 關鍵：給 switchover 時間完成

# 用 for: 5m 偵測 switchover 卡住
- alert: MariaDBSwitchoverStuck
  expr: |
    kube_endpoint_address_available{endpoint=~".*-primary"} == 0
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "MariaDB Primary endpoint 持續不可用超過 5 分鐘"
    description: |
      可能 switchover 卡住了。
      檢查：kubectl get mdb -o jsonpath='{.status.conditions}'
      參考：concepts/09-switchover.md
```

---

## 五、完整 Alert 總覽

| Alert | 層級 | 偵測什麼 | for | Severity |
|-------|------|---------|-----|----------|
| MariaDBPrimaryEndpointDown | Endpoint | Primary 不可用 | 1m | critical |
| MariaDBAllEndpointsDown | Endpoint | 整個叢集不可用 | 1m | critical |
| MariaDBSwitchoverStuck | Endpoint | Primary 長時間不可用 | 5m | critical |
| MariaDBPrimaryPodNotReady | Pod | Primary pod 不健康 | 30s | warning |
| MariaDBMultiplePodsNotReady | Pod | 多 pod 同時異常 | 1m | critical |
| MariaDBCRNotReady | CR | Operator 判斷不可用 | 2m | critical |
