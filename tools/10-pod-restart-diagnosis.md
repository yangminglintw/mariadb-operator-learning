# Pod Restart 診斷指南

## 背景

MariaDB pod 在 Kubernetes 中重啟，可能是 OOM、應用 crash、probe 失敗、storage 問題、node 問題，或 operator 主動觸發。
本指南提供系統性的診斷流程，從「發現異常」到「定位根因」。

**mariadb-operator 環境的特殊性**：
- StatefulSet 管理 pod — pod 名稱固定（`<sts>-0`, `<sts>-1`, ...），PVC 不會因重啟而改變
- Operator 會主動觸發 pod 重啟（rolling update、switchover、failover）
- Liveness/readiness probe 由 operator 設定，失敗時會導致 container 被 kill 或 pod 從 service 移除

---

## 零、怎麼發現 pod 重啟了？

你不一定會先看到 `restartCount > 0`。以下是常見的 5 個發現入口：

| # | 線索 | 你看到什麼 | 怎麼確認 |
|---|------|-----------|---------|
| 1 | **Pod AGE 很短** | `kubectl get pods` 某個 pod AGE 只有幾分鐘，其他 AGE 很長 | 比較同 StatefulSet 的其他 pod |
| 2 | **Primary pod 換了** | `.status.currentPrimary` 變了，可能發生了 failover/switchover | `kubectl get mariadb <name> -o jsonpath='{.status.currentPrimary}'` |
| 3 | **MariaDB uptime 很短** | 進 DB 查 `SELECT @@uptime` 回傳很小的數字 | 與 container startedAt 比較 |
| 4 | **Grafana/監控異常** | Connection drop、replication lag spike、metrics 斷點 | 查看對應時間點的 events |
| 5 | **Error log 有 startup 記錄** | `kubectl logs` 看到 MariaDB shutdown/startup 序列 | `kubectl logs --previous` 看上一次 |

### RESTARTS 欄 vs AGE — 兩種不同的「重啟」

```bash
# 先看這兩個欄位
kubectl --context=kind-mdb -n default get pods -l app.kubernetes.io/instance=mariadb-chaos -o wide
```

```
NAME              READY   STATUS    RESTARTS        AGE
mariadb-chaos-0   2/2     Running   798 (5d ago)    55d   ← AGE 長 + RESTARTS 高
mariadb-chaos-1   2/2     Running   0               3m    ← AGE 短 + RESTARTS=0
mariadb-chaos-2   2/2     Running   169 (3d ago)    55d   ← AGE 長 + RESTARTS 中
```

| 現象 | 發生了什麼 | 常見原因 |
|------|-----------|---------|
| AGE 短 + RESTARTS=0 | **Pod 被 delete 後重建**（全新 pod） | Operator rolling update、node drain、手動 delete、failover |
| AGE 長 + RESTARTS 高 | **Container 在同一個 pod 內反覆重啟** | OOM、crash、liveness probe 失敗 |
| AGE 短 + RESTARTS 高 | Pod 被重建，且重建後又有 container restart | 混合原因（例如 node 問題 + OOM） |

> **注意**：AGE 短 + RESTARTS=0 **不一定代表沒有 container restart 發生過**。
> 某些 operator 版本在 failover 時會 delete 舊 primary pod，導致 RESTARTS 歷史歸零。
> 如果同時看到 primary 換了，務必追查根因（見路線 B）。

### 區分 container restart vs pod recreate

```bash
# 查看 pod UID — 如果 UID 與你記得的不同，就是新 pod（recreate）
kubectl --context=kind-mdb -n default get pod mariadb-chaos-0 \
  -o jsonpath='uid={.metadata.uid} created={.metadata.creationTimestamp}'

# 查看 container 的重啟次數和狀態
kubectl --context=kind-mdb -n default get pod mariadb-chaos-0 \
  -o jsonpath='{range .status.containerStatuses[*]}{.name}{"\t restarts="}{.restartCount}{"\t started="}{.state.running.startedAt}{"\n"}{end}'
```

**關鍵概念**：
- **Container restart**：同一個 pod UID，`restartCount` 增加，pod AGE 不變。kubelet 在原地重啟 container。
- **Pod recreate**：新的 pod UID，AGE 重置，`restartCount` 從 0 開始。StatefulSet controller 建了一個全新 pod。

### 確認 primary 是否有變

```bash
# 查看目前的 primary
kubectl --context=kind-mdb -n default get mariadb mariadb-chaos \
  -o jsonpath='currentPrimary={.status.currentPrimary}'

# 檢查 operator logs 是否有 switchover/failover 記錄
OPERATOR_POD=$(kubectl --context=kind-mdb -n mariadb-operator-system get pods \
  -l control-plane=mariadb-operator-controller-manager -o name | head -1)
kubectl --context=kind-mdb -n mariadb-operator-system logs "$OPERATOR_POD" --since=1h | \
  grep -i -E "switchover|failover"
```

確認完「發生了什麼」之後，根據 RESTARTS 和 AGE 進入對應的診斷路線：

- **RESTARTS > 0** → 流程圖**路線 A**（Container restart）
- **AGE 短 + RESTARTS=0** → 流程圖**路線 B**（Pod recreate）
- **兩者都有** → 先走路線 A，再走路線 B

---

## 診斷流程圖

```
你發現了異常（見 §零 的 5 個入口）
│
│  先看 kubectl get pods 的 RESTARTS 和 AGE
│
├─ 路線 A：RESTARTS > 0（container 在同一個 pod 內重啟）
│   │
│   ├─ kubectl describe pod → lastState.terminated
│   │   │
│   │   ├─ reason=OOMKilled, exitCode=137
│   │   │   └→ §3.1：記憶體不足（InnoDB buffer pool、查詢暴衝）
│   │   │
│   │   ├─ reason=Error, exitCode=1
│   │   │   └→ §3.2：MariaDB crash（config 錯誤、InnoDB corruption）
│   │   │
│   │   ├─ reason=Error, exitCode=137（非 OOMKilled）
│   │   │   └→ §3.3 或 §3.5：Liveness probe 失敗 或 node eviction（SIGKILL）
│   │   │
│   │   ├─ reason=Error, exitCode=139
│   │   │   └→ §3.2：SIGSEGV（MariaDB bug 或記憶體損壞）
│   │   │
│   │   ├─ reason=Error, exitCode=143
│   │   │   └→ §4.1：SIGTERM 未正常處理（graceful shutdown 失敗）
│   │   │
│   │   ├─ reason=Completed, exitCode=0
│   │   │   └→ §3.6：正常退出後被重啟（operator rolling update）
│   │   │
│   │   ├─ reason=Unknown, exitCode=255
│   │   │   └→ §3.5：Container runtime 異常、node 重啟、kubelet restart
│   │   │
│   │   └─ 無 lastState → 檢查 events
│   │
│   ├─ kubectl get events --field-selector
│   │   │
│   │   ├─ "Liveness probe failed" → §3.3
│   │   ├─ "Evicted" → §3.5
│   │   ├─ "Multi-Attach error" → §3.4（參考 tools/07）
│   │   └─ "Killing" / "Stopping container" → §3.6
│   │
│   └─ kubectl logs --previous → §4：MariaDB error log 分析
│
└─ 路線 B：AGE 短 + RESTARTS=0（pod 被 delete 後重建）
    │
    │  ⚠️ RESTARTS=0 不一定代表沒有 container restart 發生過！
    │  某些 operator 版本 failover 時會 delete pod → RESTARTS 歸零
    │
    ├─ 是否剛修改過 MariaDB CR spec？
    │   └→ 是 → §3.6 Operator rolling update（通常是正常行為）
    │
    ├─ Primary 是否換了？（kubectl get mariadb -o jsonpath='{.status.currentPrimary}'）
    │   └→ 是 → §3.6 Switchover 或 Failover
    │       │
    │       └→ ⚠️ 追查根因：為什麼 failover？
    │           ├─ 查 operator logs：grep failover 原因
    │           ├─ 查 Max_used_connections 是否接近 max_connections → §3.3
    │           └─ 查 kubectl logs --previous（如果還有上次的 log）
    │
    ├─ Node 是否有問題？（kubectl describe node → Conditions）
    │   └→ 是 → §3.5 Node drain / eviction / NotReady
    │
    ├─ 是否有人手動 delete pod？
    │   └→ 是 → 正常行為（StatefulSet 會自動重建）
    │
    └─ 以上都不是 → 檢查 operator logs（§五）
```

---

## 一、快速分類（Triage）

確認是 **container restart（RESTARTS > 0）** 後，用以下命令查詳細的終止原因和 exit code。
（如果是 pod recreate，見流程圖路線 B。）

### 1.1 檢查終止原因與 exit code

```bash
# 取得特定 pod 所有 container 的重啟資訊
# 注意：mariadb-operator pod 有 2 個 container（agent + mariadb），需要看兩個
kubectl --context=kind-mdb -n default get pod mariadb-chaos-0 \
  -o jsonpath='{range .status.containerStatuses[*]}{.name}{"\t"}{.restartCount}{"\t"}{.lastState.terminated.reason}{"\t"}{.lastState.terminated.exitCode}{"\t"}{.lastState.terminated.finishedAt}{"\n"}{end}'

# 一次看所有 pod 的 lastState（mariadb container）
# 注意：containerStatuses 順序不固定，用 jsonpath 的 ?(@.name=="mariadb") 過濾
for i in 0 1 2; do
  echo "=== mariadb-chaos-$i ==="
  kubectl --context=kind-mdb -n default get pod mariadb-chaos-$i \
    -o jsonpath='{.status.containerStatuses[?(@.name=="mariadb")].restartCount}' | xargs -I{} printf "  mariadb restarts=%s " {}
  kubectl --context=kind-mdb -n default get pod mariadb-chaos-$i \
    -o jsonpath='reason={.status.containerStatuses[?(@.name=="mariadb")].lastState.terminated.reason} exitCode={.status.containerStatuses[?(@.name=="mariadb")].lastState.terminated.exitCode}'
  echo ""
  kubectl --context=kind-mdb -n default get pod mariadb-chaos-$i \
    -o jsonpath='{.status.containerStatuses[?(@.name=="agent")].restartCount}' | xargs -I{} printf "  agent   restarts=%s "  {}
  kubectl --context=kind-mdb -n default get pod mariadb-chaos-$i \
    -o jsonpath='reason={.status.containerStatuses[?(@.name=="agent")].lastState.terminated.reason} exitCode={.status.containerStatuses[?(@.name=="agent")].lastState.terminated.exitCode}'
  echo ""
done
```

### 1.2 檢查 Pod Events

```bash
# 特定 pod 的 events（按時間排序）
kubectl --context=kind-mdb -n default get events \
  --field-selector involvedObject.name=mariadb-chaos-0 \
  --sort-by='.lastTimestamp'

# 所有 Warning events（快速掃描異常）
kubectl --context=kind-mdb -n default get events \
  --field-selector type=Warning \
  --sort-by='.lastTimestamp'
```

**注意**：
- K8s events 預設只保留 **1 小時**。如果重啟發生較早，events 可能已消失。
- mariadb-operator 的 pod 有 **2 個 container**：`mariadb`（DB 本身）和 `agent`（health check sidecar，port 5566）。兩者都可能獨立重啟，注意區分 `containerStatuses` 中的名稱。

### 1.3 檢查 Node 狀態

```bash
# 找到 pod 所在的 node
NODE=$(kubectl --context=kind-mdb -n default get pod mariadb-chaos-0 -o jsonpath='{.spec.nodeName}')

# 檢查 node conditions（MemoryPressure、DiskPressure、PIDPressure）
kubectl --context=kind-mdb describe node "$NODE" | grep -A 5 "Conditions:"
```

---

## 二、Exit Code 對照表

| Exit Code | Terminated Reason | Signal | 含義 | 常見原因 | 下一步 |
|-----------|------------------|--------|------|---------|--------|
| 0 | Completed | — | 正常退出 | Operator rolling update、手動 delete pod | §3.6 |
| 1 | Error | — | 應用錯誤退出 | Config 語法錯誤、InnoDB corruption、權限問題 | §3.2 |
| 137 | OOMKilled | SIGKILL | 超出 memory limit | InnoDB buffer pool 太大、查詢暴衝 | §3.1 |
| 137 | Error | SIGKILL | 被強制終止（非 OOM） | Liveness probe 失敗、preStop 超時、eviction | §3.3/§3.5 |
| 139 | Error | SIGSEGV | 記憶體存取違規 | MariaDB bug、plugin crash | §3.2 |
| 143 | Error | SIGTERM | 正常終止信號 | Graceful shutdown 超時被 escalate | §4.1 |
| 255 | Unknown | — | 異常退出 | Container runtime 異常、node 重啟、kubelet restart | §3.5 |

**如何區分 exitCode=137 的兩種情況**：
- `reason=OOMKilled` → 確定是 OOM
- `reason=Error` + `exitCode=137` → 是 SIGKILL 但非 OOM（可能是 liveness probe 失敗後 kubelet 強殺，或 node eviction）

---

## 三、常見重啟原因與診斷

### 3.1 OOMKilled（Exit Code 137，reason=OOMKilled）

**特徵**：
- `kubectl describe pod` 顯示 `Reason: OOMKilled`
- Container 突然消失，無 graceful shutdown 日誌

**診斷命令**：

```bash
# 確認 OOM
kubectl --context=kind-mdb -n default get pod mariadb-chaos-0 \
  -o jsonpath='{.status.containerStatuses[?(@.name=="mariadb")].lastState.terminated.reason}'
# 輸出：OOMKilled

# 檢查 memory limit
kubectl --context=kind-mdb -n default get pod mariadb-chaos-0 \
  -o jsonpath='{.spec.containers[0].resources.limits.memory}'

# 檢查 InnoDB buffer pool size
kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- \
  mariadb -u root -p"$(kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- printenv MARIADB_ROOT_PASSWORD)" \
  -N -e "SELECT @@innodb_buffer_pool_size / 1024 / 1024 AS buffer_pool_mb, @@max_connections, @@innodb_buffer_pool_size / 1024 / 1024 + @@max_connections * 2 AS estimated_min_mb" 2>/dev/null
```

**根因分析**：

MariaDB 記憶體組成：

```
總記憶體 ≈ innodb_buffer_pool_size
         + max_connections × per_thread_memory（~2-5MB）
         + 其他 global buffers（key_buffer, query_cache, tmp_table...）
         + OS overhead（~100-200MB）
```

**解決方向**：
1. 增加 `resources.limits.memory`
2. 降低 `innodb_buffer_pool_size`（建議設為 memory limit 的 50-70%）
3. 降低 `max_connections`
4. 檢查是否有大查詢消耗過多 per-thread memory（`sort_buffer_size`, `join_buffer_size`）

### 3.2 CrashLoopBackOff（Exit Code 1 或 139）

**特徵**：
- Pod 狀態顯示 `CrashLoopBackOff`
- restartCount 持續增加
- Back-off 間隔越來越長（10s → 20s → 40s → ... → 5min）

**診斷命令**：

```bash
# 看上一次 crash 的日誌（最重要的一步）
kubectl --context=kind-mdb -n default logs mariadb-chaos-0 -c mariadb --previous

# 如果 --previous 也沒有有用資訊，看 init container logs
kubectl --context=kind-mdb -n default logs mariadb-chaos-0 -c init-mariadb --previous 2>/dev/null

# 檢查 datadir 權限
kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- \
  ls -la /var/lib/mysql/ 2>/dev/null
```

**常見 crash 原因**：

| 症狀（logs --previous 中的關鍵字） | 原因 | 解決 |
|--------------------------------------|------|------|
| `[ERROR] ... unknown variable` | my.cnf 有不支援的參數 | 修正 MariaDB CR 的 myCnf |
| `[ERROR] InnoDB: Unable to lock` | datadir 被佔用 | 確認舊 pod 已完全終止 |
| `[ERROR] InnoDB: Tablespace ... not found` | 資料損壞 | InnoDB recovery（§4.2） |
| `[ERROR] Can't start server: Bind on TCP/IP port` | port 衝突 | 檢查是否有殭屍 process |
| `Fatal error: Can't open and lock privilege tables` | 系統表損壞 | mariadb-install-db 或 restore |
| Segmentation fault（exitCode=139） | MariaDB bug | 升級版本、回報 bug |

### 3.3 Liveness Probe 失敗

**特徵**：
- Events 中出現 `Liveness probe failed`
- `exitCode=137`，但 `reason=Error`（非 OOMKilled）
- Pod 反覆重啟但沒有 crash log

**mariadb-operator 的 Probe 機制**：

Operator 透過 sidecar `agent` container（port 5566）提供 health check endpoint：
- Liveness: `GET /liveness` on port 5566
- Readiness: `GET /readiness` on port 5566

Agent container 內部連線 MariaDB 檢查健康狀態，再透過 HTTP 回報給 kubelet。

**診斷命令**：

```bash
# 檢查 probe 配置
kubectl --context=kind-mdb -n default get pod mariadb-chaos-0 \
  -o jsonpath='{.spec.containers[0].livenessProbe}' | python3 -m json.tool

# 檢查 events 中的 probe 失敗記錄
kubectl --context=kind-mdb -n default get events \
  --field-selector involvedObject.name=mariadb-chaos-0,reason=Unhealthy \
  --sort-by='.lastTimestamp'

# 手動測試 probe endpoint（透過 port-forward）
kubectl --context=kind-mdb -n default port-forward mariadb-chaos-0 5566:5566 &
curl -s http://localhost:5566/liveness ; echo
curl -s http://localhost:5566/readiness ; echo
kill %1 2>/dev/null

# 檢查 agent container logs（probe 失敗的詳細原因）
kubectl --context=kind-mdb -n default logs mariadb-chaos-0 -c agent --tail=50
```

**根因分析**：
- **agent container crash**：agent sidecar 本身 crash，probe endpoint 無法回應 → liveness 失敗
- **max_connections 已滿**：agent 需要連線到 MariaDB 檢查健康，如果連線數用完 → probe 失敗
- **Long-running query**：大查詢或 DDL 鎖住整個 DB，agent 的 health check query 無法回應
- **InnoDB recovery 中**：重啟後 InnoDB crash recovery 可能需要很長時間，probe 在 recovery 完成前就超時
- **probe timeout 太短**：預設 `timeoutSeconds=5`，DB 負載高時不夠

**解決方向**：
1. 增加 probe 的 `timeoutSeconds` 和 `failureThreshold`
2. 增加 `max_connections`（預留幾個給 probe 和 operator）
3. 設定 `initialDelaySeconds` 足夠長，讓 InnoDB recovery 有時間完成

#### max_connections 導致的連鎖 Failover

這是一個常見但難以診斷的場景，因為**根因的證據會被 failover 過程抹掉**。

**連鎖反應流程**：

```
max_connections 滿（App 連線暴衝）
    ↓
agent 無法連線 MariaDB → /liveness 回報失敗
    ↓
kubelet 殺掉 mariadb container（RESTARTS=1）
    ↓
operator 偵測到 primary NotReady → 觸發 failover
    ↓
┌─────────────────────────────────────────────────────────┐
│  Failover 行為因 operator 版本而異：                      │
│                                                          │
│  Community 版：舊 primary 降級為 replica                  │
│    → pod 保留，RESTARTS=1 可見                           │
│    → 但有已知 bug #363：exit(0) 可能卡在 Succeeded 狀態  │
│                                                          │
│  某些客製版：delete 舊 primary pod → 重建                 │
│    → AGE 短 + RESTARTS=0（痕跡消失）                     │
└─────────────────────────────────────────────────────────┘
    ↓
最終現象：primary 換了 + 舊 pod AGE 短或角色變了
根因（max_connections）隱藏在中間
```

**Operator Failover 行為差異**：

| | Community 版 | 某些客製版 |
|--|-------------|-----------|
| 舊 primary pod | 保留，降級為 replica | Delete 後重建 |
| RESTARTS 歷史 | **保留**（可見 RESTARTS=1） | **歸零**（AGE 短 + RESTARTS=0） |
| 已知問題 | [#363](https://github.com/mariadb-operator/mariadb-operator/issues/363)：exit(0) 卡 Succeeded | — |
| 診斷難度 | 較低（有 RESTARTS 證據） | 較高（證據消失） |

**診斷命令**（確認是否 max_connections 造成的）：

```bash
MYSQL_PWD=$(kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- printenv MARIADB_ROOT_PASSWORD)

# 歷史最高連線數 vs limit
kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- \
  mariadb -u root -p"${MYSQL_PWD}" -e "
    SELECT @@max_connections AS max_conn,
           (SELECT VARIABLE_VALUE FROM information_schema.GLOBAL_STATUS
            WHERE VARIABLE_NAME = 'Max_used_connections') AS max_used,
           (SELECT VARIABLE_VALUE FROM information_schema.GLOBAL_STATUS
            WHERE VARIABLE_NAME = 'Connection_errors_max_connections') AS errors_max_conn
  " 2>/dev/null

# 如果 max_used ≈ max_connections，或 errors_max_conn > 0
# → max_connections 曾經滿過，很可能是根因
```

**預防**：
- `max_connections` 預留至少 5-10 個給 agent probe、operator、monitoring
- 監控 `Max_used_connections / max_connections` 比率（見 §七 PromQL）
- App 端使用 connection pool，設定合理的 pool size 上限

### 3.4 Storage 問題

#### Disk Full

**特徵**：
- MariaDB error log 顯示 `disk full` 或 `No space left on device`
- Binlog 或 data 持續增長

**診斷命令**：

```bash
# 檢查容器內磁碟使用量
kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- \
  df -h /var/lib/mysql

# 檢查大檔案
kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- \
  sh -c "du -sh /var/lib/mysql/* 2>/dev/null | sort -rh | head -10"

# 檢查 binlog 佔用
kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- \
  mariadb -u root -p"$(kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- printenv MARIADB_ROOT_PASSWORD)" \
  -e "SHOW BINARY LOGS" 2>/dev/null
```

**解決方向**：
1. 清理過期 binlog：`PURGE BINARY LOGS BEFORE NOW() - INTERVAL 3 DAY`
2. 擴展 PVC（如果 StorageClass 支援 `allowVolumeExpansion`）
3. 調整 `expire_logs_days` 或 `binlog_expire_logs_seconds`

#### Multi-Attach Error

Pod 卡在 `ContainerCreating`，events 顯示 `Multi-Attach error for volume`。

詳細說明見 [`tools/07-pvc-multiattach-promql.md`](./07-pvc-multiattach-promql.md) 和 [`tools/09-pv-va-storage-guide.md`](./09-pv-va-storage-guide.md)。

### 3.5 Node 問題（Eviction / NotReady）

**特徵**：
- Pod 被驅逐（Evicted），events 顯示 `The node was low on resource: memory`
- `exitCode=137`，`reason=Error`（非 OOMKilled — 是 node-level eviction，不是 container-level OOM）
- Node 狀態顯示 `MemoryPressure=True` 或 `DiskPressure=True`

**診斷命令**：

```bash
# 檢查 node conditions
NODE=$(kubectl --context=kind-mdb -n default get pod mariadb-chaos-0 -o jsonpath='{.spec.nodeName}')
kubectl --context=kind-mdb describe node "$NODE" | grep -E "Conditions:|MemoryPressure|DiskPressure|PIDPressure"

# 檢查 node 上的 eviction events
kubectl --context=kind-mdb get events -A --field-selector reason=Evicted --sort-by='.lastTimestamp'

# 檢查 node 資源使用
kubectl --context=kind-mdb top node "$NODE"
```

**解決方向**：
1. 增加 node 資源（scale up）或增加 node 數量（scale out）
2. 設定適當的 `resources.requests` 確保 QoS class 至少為 Burstable
3. 設定 `PriorityClass` 確保 DB pod 不會被優先驅逐

Failover 相關流程見 [`concepts/10-failover.md`](../concepts/10-failover.md)。

### 3.6 Operator 觸發的重啟

**特徵**：
- restartCount 增加 1（通常只有一次，不像 crash 會持續增加）
- 時間點對應 MariaDB CR spec 的變更、switchover、或 failover
- `exitCode=0`（rolling update）或 pod 被 delete/recreate

**三種 Operator 觸發的重啟**：

| 觸發原因 | 機制 | 特徵 |
|----------|------|------|
| Rolling update | 修改 MariaDB CR spec → operator 更新 StatefulSet → 逐一重建 pod | exitCode=0，pods 依序重啟 |
| Switchover | 手動修改 `spec.replication.primary.podIndex` | 涉及 primary 切換，operator logs 有 switchover 記錄 |
| Failover | Primary pod NotReady → operator 自動選新 primary | 詳見 `concepts/10-failover.md` |

**Failover 後舊 primary 的處理（因 operator 版本而異）**：

| | Community 版 | 某些客製版 |
|--|-------------|-----------|
| 舊 primary | 降級為 replica，**pod 保留** | **Delete pod** → StatefulSet 重建 |
| RESTARTS | 保留（container restart 歷史可見） | 歸零（歷史消失） |
| AGE | 不變 | 重置（AGE 很短） |
| 根因追查 | 較容易（有 lastState） | 需查 operator logs 和 `Max_used_connections` |

> Community 版已知問題 [#363](https://github.com/mariadb-operator/mariadb-operator/issues/363)：
> Liveness probe 失敗後，如果 container 以 `exit(0)` 退出，Pod 會卡在 `Succeeded` 狀態，
> Kubernetes 不會自動重啟它，導致 failover 無法完成。需手動驅逐 pod。

**診斷命令**：

```bash
# 檢查 MariaDB CR 最近的 spec 變更
kubectl --context=kind-mdb -n default get mariadb mariadb-chaos \
  -o jsonpath='{.metadata.resourceVersion}'

# 檢查 StatefulSet 的 update 歷史
kubectl --context=kind-mdb -n default rollout history sts mariadb-chaos

# 檢查 operator logs 中的 reconcile 記錄
OPERATOR_POD=$(kubectl --context=kind-mdb -n mariadb-operator-system get pods -l control-plane=mariadb-operator-controller-manager -o name | head -1)
kubectl --context=kind-mdb -n mariadb-operator-system logs "$OPERATOR_POD" --since=1h | grep -i "mariadb-chaos"

# 檢查 switchover/failover 相關 events
kubectl --context=kind-mdb -n default get events \
  --field-selector reason=Failover --sort-by='.lastTimestamp' 2>/dev/null
kubectl --context=kind-mdb -n default get events \
  --field-selector reason=Switchover --sort-by='.lastTimestamp' 2>/dev/null
```

---

## 四、MariaDB 專屬診斷

### 4.1 Error Log 檢查

MariaDB error log 是最重要的診斷來源。重啟後先看上一次的 log。

```bash
# 方法 1：kubectl logs --previous（推薦，最簡單）
kubectl --context=kind-mdb -n default logs mariadb-chaos-0 -c mariadb --previous --tail=100

# 方法 2：從容器內讀取 error log 檔案
# 先取得 log 路徑
LOG_ERROR=$(kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- \
  mariadb -u root -p"$(kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- printenv MARIADB_ROOT_PASSWORD)" \
  -N -e "SELECT @@log_error" 2>/dev/null)
echo "Error log path: $LOG_ERROR"

# 讀取最後 50 行
kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- \
  tail -50 "$LOG_ERROR" 2>/dev/null
```

**關鍵 pattern**：

| Error Log 關鍵字 | 含義 | 對應原因 |
|-------------------|------|---------|
| `[ERROR] InnoDB: Fatal error` | InnoDB 致命錯誤 | §3.2 |
| `Got signal 11` | SIGSEGV | §3.2 |
| `Got signal 6` | SIGABRT | assert failure |
| `InnoDB: Database was not shutdown normally` | 上次非正常關閉 | §4.2 |
| `[ERROR] Aborting` | MariaDB 主動退出 | 檢查前面的錯誤訊息 |
| `[Warning] Aborted connection` | 連線被中斷 | client 端問題或 network |
| `Disk is full` | 磁碟滿 | §3.4 |

### 4.2 InnoDB Recovery 偵測

非正常關閉後（OOM、SIGKILL、node crash），InnoDB 會自動執行 crash recovery。

```bash
# 檢查目前的 error log 是否有 recovery 記錄
kubectl --context=kind-mdb -n default logs mariadb-chaos-0 -c mariadb --tail=200 | \
  grep -i -E "recovery|crash|shutdown normally"
```

**Recovery 日誌解讀**：

```
# 正常的 crash recovery（通常很快）
InnoDB: Database was not shutdown normally!
InnoDB: Starting crash recovery.
InnoDB: ... recovered ...
InnoDB: Crash recovery finished.

# 需要注意的（recovery 時間可能很長）
InnoDB: Starting crash recovery from checkpoint LSN=...
InnoDB: Doing recovery: scanned up to log sequence number ...
```

**如果 recovery 時間太長，Liveness probe 可能在 recovery 完成前觸發 → 又一次 SIGKILL → 無限重啟循環**。
解決：增加 `initialDelaySeconds` 或使用 `startupProbe`。

### 4.3 Replication 斷線

重啟後 replica 需要重新連線到 primary。如果 binlog 位置不一致，可能出錯。

```bash
# 檢查 replication 狀態
MYSQL_PWD=$(kubectl --context=kind-mdb -n default exec mariadb-chaos-0 -c mariadb -- printenv MARIADB_ROOT_PASSWORD)

for i in 0 1 2; do
  echo "=== mariadb-chaos-$i ==="
  kubectl --context=kind-mdb -n default exec mariadb-chaos-$i -c mariadb -- \
    mariadb -u root -p"${MYSQL_PWD}" -e "SHOW SLAVE STATUS\G" 2>/dev/null | \
    grep -E "Slave_IO_Running|Slave_SQL_Running|Last_IO_Error|Last_SQL_Error|Seconds_Behind_Master"
  echo ""
done
```

Replication 問題的深度排查見 [`concepts/11-replication-debugging.md`](../concepts/11-replication-debugging.md)。

---

## 五、Operator 層級診斷

### 5.1 Operator Logs

```bash
# 找到 operator pod
OPERATOR_POD=$(kubectl --context=kind-mdb -n mariadb-operator-system get pods \
  -l control-plane=mariadb-operator-controller-manager -o name | head -1)

# 看最近 1 小時的 operator logs（過濾目標 MariaDB）
kubectl --context=kind-mdb -n mariadb-operator-system logs "$OPERATOR_POD" --since=1h | \
  grep "mariadb-chaos"

# 看 error level logs
kubectl --context=kind-mdb -n mariadb-operator-system logs "$OPERATOR_POD" --since=1h | \
  grep -i "error\|fail"
```

### 5.2 MariaDB CR Status 檢查

```bash
# 完整 status
kubectl --context=kind-mdb -n default get mariadb mariadb-chaos -o yaml | \
  grep -A 50 "^status:"

# 快速檢查 conditions
kubectl --context=kind-mdb -n default get mariadb mariadb-chaos \
  -o jsonpath='{range .status.conditions[*]}{.type}{"\t"}{.status}{"\t"}{.message}{"\n"}{end}'

# 檢查 currentPrimary
kubectl --context=kind-mdb -n default get mariadb mariadb-chaos \
  -o jsonpath='currentPrimary={.status.currentPrimary}'
```

**Condition 狀態判讀**：

| Condition | Status=True | Status=False |
|-----------|-------------|--------------|
| Ready | MariaDB 正常運作 | 有問題，檢查 message |
| PrimarySwitched | Switchover 完成 | Switchover 進行中或未觸發 |

更多 Status 說明見 [`concepts/06-status.md`](../concepts/06-status.md)。

---

## 六、診斷腳本

### 腳本 1：Pod Restart 報告

```bash
#!/bin/bash
# pod_restart_report.sh
# 用法：./pod_restart_report.sh <context> <namespace> <statefulset>
# 輸出所有 pod 的重啟資訊：restartCount、lastState、recent events、MariaDB uptime

CONTEXT="$1"
NAMESPACE="$2"
STS_NAME="$3"

if [ -z "$STS_NAME" ]; then
  echo "Usage: $0 <context> <namespace> <statefulset>"
  exit 1
fi

# Pod 名稱從 StatefulSet replicas 數生成
REPLICAS=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get sts "${STS_NAME}" \
  -o jsonpath='{.spec.replicas}')
if [ -z "$REPLICAS" ] || [ "$REPLICAS" -eq 0 ]; then
  echo "Error: StatefulSet ${STS_NAME} not found or has 0 replicas"
  exit 1
fi

# 從容器環境變數取得 root 密碼
MYSQL_PWD=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
  exec "${STS_NAME}-0" -c mariadb -- printenv MARIADB_ROOT_PASSWORD 2>/dev/null)

# 從 CR status 取得 Primary pod 名稱
CURRENT_PRIMARY=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
  get mariadb "${STS_NAME}" -o jsonpath='{.status.currentPrimary}' 2>/dev/null)

get_role() {
  if [ "$1" = "$CURRENT_PRIMARY" ]; then
    echo "Primary"
  else
    echo "Replica"
  fi
}

echo "============================================"
echo "Pod Restart Report: ${STS_NAME}"
echo "Context: ${CONTEXT} | Namespace: ${NAMESPACE}"
echo "Primary: ${CURRENT_PRIMARY:-unknown}"
echo "============================================"
echo ""

for i in $(seq 0 $((REPLICAS - 1))); do
  POD="${STS_NAME}-${i}"
  ROLE=$(get_role "$POD")

  echo "--- ${POD} (${ROLE}) ---"

  # Container status
  STATUS_JSON=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pod "${POD}" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="mariadb")]}' 2>/dev/null)

  if [ -z "$STATUS_JSON" ]; then
    echo "  [Pod not found or not running]"
    echo ""
    continue
  fi

  RESTART_COUNT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pod "${POD}" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="mariadb")].restartCount}')
  LAST_REASON=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pod "${POD}" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="mariadb")].lastState.terminated.reason}')
  LAST_EXIT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pod "${POD}" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="mariadb")].lastState.terminated.exitCode}')
  LAST_TIME=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pod "${POD}" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="mariadb")].lastState.terminated.finishedAt}')
  STARTED_AT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pod "${POD}" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="mariadb")].state.running.startedAt}')

  printf "  %-20s %s\n" "Restarts:" "${RESTART_COUNT}"
  if [ -n "$LAST_REASON" ]; then
    printf "  %-20s %s (exitCode=%s)\n" "Last termination:" "${LAST_REASON}" "${LAST_EXIT}"
    printf "  %-20s %s\n" "Terminated at:" "${LAST_TIME}"
  else
    printf "  %-20s %s\n" "Last termination:" "(none recorded)"
  fi
  printf "  %-20s %s\n" "Current start:" "${STARTED_AT}"

  # MariaDB uptime（與 container start time 比較）
  if [ -n "$MYSQL_PWD" ]; then
    UPTIME=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
      mariadb -u root -p"${MYSQL_PWD}" -N -e \
      "SELECT CONCAT(FLOOR(@@uptime/3600), 'h ', FLOOR(MOD(@@uptime,3600)/60), 'm ', MOD(@@uptime,60), 's') AS uptime" 2>/dev/null)
    if [ -n "$UPTIME" ]; then
      printf "  %-20s %s\n" "MariaDB uptime:" "${UPTIME}"
    fi
  fi

  # Recent events（最近 5 筆 Warning）
  EVENTS=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get events \
    --field-selector "involvedObject.name=${POD},type=Warning" \
    --sort-by='.lastTimestamp' 2>/dev/null | tail -6 | head -5)
  if [ -n "$EVENTS" ]; then
    echo "  Recent warnings:"
    echo "$EVENTS" | while IFS= read -r line; do
      echo "    $line"
    done
  fi

  echo ""
done

# Summary
echo "============================================"
echo "Summary"
echo "============================================"
TOTAL_RESTARTS=0
for i in $(seq 0 $((REPLICAS - 1))); do
  POD="${STS_NAME}-${i}"
  RC=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pod "${POD}" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="mariadb")].restartCount}' 2>/dev/null)
  RC=${RC:-0}
  TOTAL_RESTARTS=$((TOTAL_RESTARTS + RC))
  REASON=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pod "${POD}" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="mariadb")].lastState.terminated.reason}' 2>/dev/null)
  printf "  %-20s restarts=%-3s last_reason=%s\n" "${POD}" "${RC}" "${REASON:-(none)}"
done
echo ""
echo "  Total restarts across all pods: ${TOTAL_RESTARTS}"
```

### 腳本 2：OOM 風險檢查

```bash
#!/bin/bash
# check_oom.sh
# 用法：./check_oom.sh <context> <namespace> <statefulset>
# 檢查每個 pod 的記憶體配置 vs MariaDB 記憶體使用，評估 OOM 風險

CONTEXT="$1"
NAMESPACE="$2"
STS_NAME="$3"

if [ -z "$STS_NAME" ]; then
  echo "Usage: $0 <context> <namespace> <statefulset>"
  exit 1
fi

REPLICAS=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get sts "${STS_NAME}" \
  -o jsonpath='{.spec.replicas}')
if [ -z "$REPLICAS" ] || [ "$REPLICAS" -eq 0 ]; then
  echo "Error: StatefulSet ${STS_NAME} not found or has 0 replicas"
  exit 1
fi

MYSQL_PWD=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" \
  exec "${STS_NAME}-0" -c mariadb -- printenv MARIADB_ROOT_PASSWORD 2>/dev/null)

echo "============================================"
echo "OOM Risk Check: ${STS_NAME}"
echo "============================================"
echo ""

for i in $(seq 0 $((REPLICAS - 1))); do
  POD="${STS_NAME}-${i}"
  echo "--- ${POD} ---"

  # Container memory limit
  MEM_LIMIT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pod "${POD}" \
    -o jsonpath='{.spec.containers[0].resources.limits.memory}' 2>/dev/null)
  MEM_REQUEST=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pod "${POD}" \
    -o jsonpath='{.spec.containers[0].resources.requests.memory}' 2>/dev/null)

  printf "  %-30s %s\n" "Memory limit:" "${MEM_LIMIT:-(not set)}"
  printf "  %-30s %s\n" "Memory request:" "${MEM_REQUEST:-(not set)}"

  # OOM 歷史
  LAST_REASON=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pod "${POD}" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="mariadb")].lastState.terminated.reason}' 2>/dev/null)
  if [ "$LAST_REASON" = "OOMKilled" ]; then
    LAST_TIME=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pod "${POD}" \
      -o jsonpath='{.status.containerStatuses[?(@.name=="mariadb")].lastState.terminated.finishedAt}' 2>/dev/null)
    echo "  *** OOM DETECTED *** last OOMKilled at: ${LAST_TIME}"
  fi

  # MariaDB 記憶體配置
  if [ -n "$MYSQL_PWD" ]; then
    RESULT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
      mariadb -u root -p"${MYSQL_PWD}" -N -e "
        SELECT
          ROUND(@@innodb_buffer_pool_size / 1024 / 1024) AS buffer_pool_mb,
          @@max_connections AS max_conn,
          ROUND(@@sort_buffer_size / 1024 / 1024, 1) AS sort_buf_mb,
          ROUND(@@join_buffer_size / 1024 / 1024, 1) AS join_buf_mb,
          ROUND(@@read_buffer_size / 1024 / 1024, 1) AS read_buf_mb,
          ROUND(@@tmp_table_size / 1024 / 1024) AS tmp_table_mb
      " 2>/dev/null)

    if [ -n "$RESULT" ]; then
      # 解析結果
      BP_MB=$(echo "$RESULT" | awk '{print $1}')
      MAX_CONN=$(echo "$RESULT" | awk '{print $2}')
      SORT_BUF=$(echo "$RESULT" | awk '{print $3}')
      JOIN_BUF=$(echo "$RESULT" | awk '{print $4}')
      READ_BUF=$(echo "$RESULT" | awk '{print $5}')
      TMP_TABLE=$(echo "$RESULT" | awk '{print $6}')

      # 估算每個連線的記憶體（sort_buf + join_buf + read_buf + ~1MB overhead）
      PER_CONN_MB=$(echo "$SORT_BUF $JOIN_BUF $READ_BUF" | awk '{printf "%.1f", $1 + $2 + $3 + 1}')
      # 最壞情況：所有連線同時活躍
      WORST_CONN_MB=$(echo "$MAX_CONN $PER_CONN_MB" | awk '{printf "%d", $1 * $2}')
      # 估算總需求
      ESTIMATED_MB=$(echo "$BP_MB $WORST_CONN_MB" | awk '{printf "%d", $1 + $2 + 200}')

      printf "  %-30s %s MB\n" "innodb_buffer_pool_size:" "${BP_MB}"
      printf "  %-30s %s\n" "max_connections:" "${MAX_CONN}"
      printf "  %-30s %s MB (sort=%s + join=%s + read=%s + 1)\n" \
        "Per-connection memory:" "${PER_CONN_MB}" "${SORT_BUF}" "${JOIN_BUF}" "${READ_BUF}"
      printf "  %-30s %s MB\n" "tmp_table_size:" "${TMP_TABLE}"
      echo ""
      echo "  Memory estimate (worst case):"
      printf "    buffer_pool:    %6s MB\n" "${BP_MB}"
      printf "    connections:    %6s MB (%s × %s MB)\n" "${WORST_CONN_MB}" "${MAX_CONN}" "${PER_CONN_MB}"
      printf "    OS overhead:    %6s MB (estimated)\n" "200"
      printf "    ─────────────────────────\n"
      printf "    Total:          %6s MB\n" "${ESTIMATED_MB}"
      echo ""

      # 與 limit 比較（簡單解析，假設 Mi 或 Gi 單位）
      if [ -n "$MEM_LIMIT" ]; then
        LIMIT_MB=""
        case "$MEM_LIMIT" in
          *Gi) LIMIT_MB=$(echo "${MEM_LIMIT%Gi}" | awk '{printf "%d", $1 * 1024}') ;;
          *Mi) LIMIT_MB="${MEM_LIMIT%Mi}" ;;
          *G)  LIMIT_MB=$(echo "${MEM_LIMIT%G}" | awk '{printf "%d", $1 * 1000}') ;;
          *M)  LIMIT_MB="${MEM_LIMIT%M}" ;;
        esac

        if [ -n "$LIMIT_MB" ]; then
          USAGE_PCT=$(echo "$ESTIMATED_MB $LIMIT_MB" | awk '{printf "%d", $1 * 100 / $2}')
          printf "  Limit: %s MB | Estimated: %s MB | Usage: %s%%\n" "$LIMIT_MB" "$ESTIMATED_MB" "$USAGE_PCT"
          if [ "$USAGE_PCT" -gt 90 ]; then
            echo "  *** HIGH OOM RISK *** estimated usage > 90% of limit"
          elif [ "$USAGE_PCT" -gt 70 ]; then
            echo "  [WARNING] estimated usage > 70% of limit"
          else
            echo "  [OK] estimated usage within safe range"
          fi
        fi
      fi
    fi
  fi

  echo ""
done
```

---

## 七、PromQL 監控與告警規則

除了被動診斷，你也可以用 Prometheus 主動偵測 pod restart 問題。

### 7.1 常用 PromQL 查詢

在 Grafana 或 Prometheus UI 中直接使用：

```promql
# 過去 1 小時的 container 重啟次數（依 pod + container 分組）
increase(kube_pod_container_status_restarts_total{namespace="default", pod=~"mariadb-chaos-.*"}[1h])

# 偵測 OOMKilled（最近一次終止原因是 OOMKilled 的 container）
kube_pod_container_status_last_terminated_reason{namespace="default", pod=~"mariadb-chaos-.*", reason="OOMKilled"} > 0

# 偵測 CrashLoopBackOff
kube_pod_container_status_waiting_reason{namespace="default", pod=~"mariadb-chaos-.*", reason="CrashLoopBackOff"} > 0

# Container 記憶體使用率 vs limit（預警 OOM）
container_memory_working_set_bytes{namespace="default", pod=~"mariadb-chaos-.*", container="mariadb"}
  /
kube_pod_container_resource_limits{namespace="default", pod=~"mariadb-chaos-.*", container="mariadb", resource="memory"}
  * 100

# Pod 是否 Ready（0 = NotReady，可能觸發 failover）
kube_pod_status_ready{namespace="default", pod=~"mariadb-chaos-.*", condition="true"} == 0
```

### 7.2 PrometheusRule YAML

以下是完整的告警規則，可直接 `kubectl apply`（需要已安裝 kube-prometheus-stack）：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: mariadb-pod-restart-alerts
  namespace: default
  labels:
    release: kube-prometheus-stack  # 依你的 Prometheus 安裝調整
spec:
  groups:
  - name: mariadb-pod-restart
    rules:

    # --- Container 重啟過多 ---
    - alert: MariaDBPodRestarting
      expr: |
        increase(
          kube_pod_container_status_restarts_total{
            namespace="default",
            pod=~"mariadb-chaos-.*",
            container="mariadb"
          }[1h]
        ) > 3
      for: 5m
      labels:
        severity: warning
      annotations:
        summary: "MariaDB container 頻繁重啟"
        description: |
          Pod {{ $labels.pod }} 的 mariadb container 在過去 1 小時內重啟了 {{ $value | printf "%.0f" }} 次。
          診斷：查看 §一 快速分類，執行 kubectl logs {{ $labels.pod }} -c mariadb --previous

    # --- OOMKilled ---
    - alert: MariaDBPodOOMKilled
      expr: |
        kube_pod_container_status_last_terminated_reason{
          namespace="default",
          pod=~"mariadb-chaos-.*",
          container="mariadb",
          reason="OOMKilled"
        } > 0
      for: 0m
      labels:
        severity: critical
      annotations:
        summary: "MariaDB container 被 OOMKilled"
        description: |
          Pod {{ $labels.pod }} 的 mariadb container 最近一次終止原因是 OOMKilled。
          診斷：見 §3.1，執行 check_oom.sh 檢查記憶體配置。

    # --- CrashLoopBackOff ---
    - alert: MariaDBPodCrashLooping
      expr: |
        kube_pod_container_status_waiting_reason{
          namespace="default",
          pod=~"mariadb-chaos-.*",
          container="mariadb",
          reason="CrashLoopBackOff"
        } > 0
      for: 5m
      labels:
        severity: critical
      annotations:
        summary: "MariaDB container 進入 CrashLoopBackOff"
        description: |
          Pod {{ $labels.pod }} 的 mariadb container 持續 crash。
          診斷：見 §3.2，執行 kubectl logs {{ $labels.pod }} -c mariadb --previous

    # --- 記憶體使用率接近 limit（OOM 預警）---
    - alert: MariaDBMemoryNearLimit
      expr: |
        (
          container_memory_working_set_bytes{
            namespace="default",
            pod=~"mariadb-chaos-.*",
            container="mariadb"
          }
          /
          kube_pod_container_resource_limits{
            namespace="default",
            pod=~"mariadb-chaos-.*",
            container="mariadb",
            resource="memory"
          }
        ) > 0.9
      for: 10m
      labels:
        severity: warning
      annotations:
        summary: "MariaDB 記憶體使用率 > 90%"
        description: |
          Pod {{ $labels.pod }} 的記憶體使用率為 {{ $value | printf "%.1f" }}%，接近 limit。
          可能即將 OOMKilled。診斷：見 §3.1，執行 check_oom.sh。

    # --- Agent sidecar 重啟（影響 health check）---
    - alert: MariaDBAgentRestarting
      expr: |
        increase(
          kube_pod_container_status_restarts_total{
            namespace="default",
            pod=~"mariadb-chaos-.*",
            container="agent"
          }[1h]
        ) > 3
      for: 5m
      labels:
        severity: warning
      annotations:
        summary: "MariaDB agent sidecar 頻繁重啟"
        description: |
          Pod {{ $labels.pod }} 的 agent container 在過去 1 小時內重啟了 {{ $value | printf "%.0f" }} 次。
          Agent 負責 liveness/readiness probe（port 5566），agent 重啟可能導致 mariadb container 也被重啟。
          診斷：見 §3.3，執行 kubectl logs {{ $labels.pod }} -c agent
```

### 7.3 Alert 與診斷章節對照

| Alert | Severity | 觸發條件 | 對應章節 |
|-------|----------|---------|---------|
| MariaDBPodRestarting | warning | 1h 內重啟 > 3 次 | §一 快速分類 |
| MariaDBPodOOMKilled | critical | 最近終止原因 = OOMKilled | §3.1 OOMKilled |
| MariaDBPodCrashLooping | critical | CrashLoopBackOff 持續 5 分鐘 | §3.2 CrashLoopBackOff |
| MariaDBMemoryNearLimit | warning | 記憶體 > 90% limit 持續 10 分鐘 | §3.1 OOMKilled（預警） |
| MariaDBAgentRestarting | warning | agent 1h 內重啟 > 3 次 | §3.3 Liveness Probe |

**注意**：以上 YAML 中的 `namespace` 和 `pod` regex 需要根據你的環境調整。PromQL 查詢需要 [kube-state-metrics](https://github.com/kubernetes/kube-state-metrics) 和 [cAdvisor](https://github.com/google/cadvisor) 提供的 metrics。

更多 PromQL 技巧見 [`tools/07-pvc-multiattach-promql.md`](./07-pvc-multiattach-promql.md)。

### 7.4 Connection Spike 偵測

#### 問題：為什麼現有的 `for` alert 會漏掉 spike？

```
現有 alert 能抓到的情況（緩慢增長）：
  connections ── 75% ── 80% ── 85% ── 持續 5 分鐘 ── alert 觸發 ✓
                                       ◄── for: 5m ──►

漏掉的情況（突然暴衝）：
  connections ── 30% ── spike 到 100% ── probe 失敗 ── failover ── 歸零
                        ◄── < 1 分鐘 ──►
                        for: 5m 還沒到就結束了 ✗

更糟的情況（兩次 scrape 之間）：
  scrape          scrape          scrape
    |               |               |
    ▼               ▼               ▼
    30%   （spike 到 100% → failover → 歸零）   5%
           ← 發生在兩次 scrape 之間 →
           Prometheus 完全沒看到 ✗✗
```

#### 解決方案：4 條互補的 alert rules

以下 alert 使用 **mysqld_exporter** 提供的 metrics（前綴 `mysql_*`）。

```yaml
# === Connection Spike Alert Rules ===
# 與現有的 0.75/0.80/0.85 + for:10m/5m/3m 搭配使用

# --- Alert 1: Instant spike (for: 0m, fires immediately) ---
# Connection ratio exceeds 95%, alert immediately without waiting
- alert: MariaDBConnectionSpike
  expr: mysql_global_status_threads_connected / mysql_global_variables_max_connections > 0.95
  for: 0m
  labels:
    severity: critical
  annotations:
    summary: "MariaDB connection spike > 95%"
    description: "{{ $labels.instance }} connection usage is {{ $value | printf \"%.1f\" }}%. Connections approaching max_connections, may trigger probe failure and failover. Check for app connection surge immediately."

# --- Alert 2: Connection surge ---
# 3 approaches below, choose the best fit for your environment (uncomment to use)
#
# Approach A: Baseline multiplier (auto-adapts to different specs)
#   Normal 40, 1.1x = 44 triggers, more sensitive than 2x threshold
#   + Must exceed 50% max to avoid low-baseline false positives
#
# Approach B: Peak in last 2 minutes (catches any scrape high point)
#   Uses max_over_time to capture max value in 2-minute window
#   Even a single scrape catching the spike will trigger
#
# Approach C: Rate of change (catches the "surging" moment)
#   Uses deriv() to detect connection count rise rate
#   Best when spike has a rising phase (not instant jump to max)

# --- Approach A: Baseline 1.1x + 50% max ---
- alert: MariaDBConnectionSurge
  expr: (mysql_global_status_threads_connected > 1.1 * avg_over_time(mysql_global_status_threads_connected[1h])) and (mysql_global_status_threads_connected / mysql_global_variables_max_connections > 0.5)
  for: 0m
  labels:
    severity: warning
  annotations:
    summary: "MariaDB connection count surge"
    description: "{{ $labels.instance }} current connections exceed 1.1x the 1-hour average and over 50% of max_connections."

# --- Approach B: Peak in 5 minutes > 70% max ---
# 5-minute window covers spikes lasting over 2 minutes
- alert: MariaDBConnectionPeak
  expr: max_over_time(mysql_global_status_threads_connected[5m]) / mysql_global_variables_max_connections > 0.7
  for: 0m
  labels:
    severity: warning
  annotations:
    summary: "MariaDB connection peak > 70% in 5 minutes"
    description: "{{ $labels.instance }} connection peak reached {{ $value | printf \"%.0f\" }}% of max_connections in the last 5 minutes."

# --- Approach C: Connection growth rate (★ best in testing) ---
# 5-minute window to catch sustained rising trends
# deriv unit is "per second", 0.03/s ≈ ~9 connections per 5 minutes
# This threshold catches real spikes better than > 1 (per second +1, too high)
# AND > 50% max filters false positives (normal small fluctuations won't trigger)
# Trade-off: may only see 1-2 data points (early spike points below 50% are filtered)
#            but combined with Alert 2a/2b/4, overall coverage is sufficient
- alert: MariaDBConnectionRising
  expr: deriv(mysql_global_status_threads_connected[5m]) > 0.03 and mysql_global_status_threads_connected / mysql_global_variables_max_connections > 0.5
  for: 0m
  labels:
    severity: warning
  annotations:
    summary: "MariaDB connections rising rapidly"
    description: "{{ $labels.instance }} connections are growing rapidly (+{{ $value | printf \"%.1f\" }}/sec) and exceed 50% of max_connections."

# --- Alert 3: Post-incident detection — connection refused counter ---
# Confirms connections were refused due to max_connections
# ⚠️ Verify your mysqld_exporter exports this metric, comment out if not available
- alert: MariaDBConnectionErrorsDetected
  expr: increase(mysql_global_status_connection_errors_max_connections[5m]) > 0
  for: 0m
  labels:
    severity: critical
  annotations:
    summary: "MariaDB max_connections refused detected"
    description: "{{ $labels.instance }} had connections refused due to max_connections in the last 5 minutes. Even if the spike has passed, this counter proves max_connections was reached."

# --- Alert 4: Historical high watermark (★ most reliable spike detection) ---
# max_used_connections = highest connection count since MariaDB started (high watermark)
# Key advantage: even if spike completes between two scrapes, the value stays at the peak
# This is the only metric that can detect spikes in scrape blind spots
- alert: MariaDBMaxUsedConnectionsHigh
  expr: mysql_global_status_max_used_connections / mysql_global_variables_max_connections > 0.9
  for: 0m
  labels:
    severity: critical
  annotations:
    summary: "MariaDB max used connections > 90%"
    description: "{{ $labels.instance }} highest connection count since startup reached {{ $value | printf \"%.0f\" }}% of max_connections. Even if the spike has passed, this high watermark proves it approached the limit. Note: resets on MariaDB restart."
```

#### Alert 搭配策略

| Alert | 偵測時機 | 能抓到 scrape 盲區的 spike？ | Severity |
|-------|---------|------|----------|
| 現有 0.75/0.80/0.85 + for | 緩慢增長 | ✗ for 來不及 | warning |
| Alert 1: **ConnectionSpike** | spike 瞬間 | △ 如果 scrape 恰好抓到 | critical |
| Alert 2a: **ConnectionSurge** | spike 上升（> 1.1× 基線 + > 50% max） | △ 如果 scrape 抓到 | warning |
| Alert 2b: **ConnectionPeak** | 5 分鐘內峰值 > 70% max | △ 如果 scrape 抓到 | warning |
| Alert 2c: **ConnectionRising** ★ | 5 分鐘斜率 > 0.03/s + > 50% max | △ 如果 scrape 抓到（實測效果最佳，無誤報） | warning |
| Alert 3: **ConnectionErrors** | spike 之後 | △ 如果 counter 被 scrape 到 | critical |
| Alert 4: **MaxUsedConnHigh** ★ | **啟動以來** | **✓ 高水位不會降，下次 scrape 一定看到** | critical |

**★ Alert 4 是唯一能可靠偵測 scrape 盲區 spike 的 alert**，因為 `max_used_connections` 是高水位標記，spike 過了也不降。

#### 事後偵測的限制

- Alert 3 和 4 需要確認你的 mysqld_exporter 有輸出對應 metric（`connection_errors_max_connections`、`max_used_connections`）
- 如果內部系統沒有這些 metric，先註解掉，未來啟用時取消註解
- MariaDB process restart 後，in-memory counter（`Connection_errors_max_connections`）歸零
- 如果 spike + failover 在兩次 scrape 之間全部完成，事後偵測也可能漏掉

#### 無法解決的盲區

即使加了以上所有 alert，仍有一個**無法用 Prometheus 解決的盲區**：

```
如果 spike 發生並完成在兩次 scrape 之間（例如 30 秒或 120 秒的間隔內）：
→ Alert 1（threshold）：沒抓到
→ Alert 2（surge）：avg_over_time 來不及反映，或 spike 太短
→ Alert 3（counter）：counter 增加沒被 scrape
→ Alert 4（高水位）：可能抓到（Max_used_connections 值會保留直到 restart）✓

唯一保證能事後發現的方法：
→ 查 operator logs 的 failover 記錄
→ 查 MariaDB error log 的 "Too many connections"
→ 查 SHOW GLOBAL STATUS LIKE 'Max_used_connections'（手動 SQL）
```

---

## 八、常見場景速查表

### Container Restart（RESTARTS > 0）

| 你看到什麼 | 最可能的原因 | 查什麼 |
|-----------|-------------|--------|
| restartCount 持續增加，reason=OOMKilled | 記憶體不足 | §3.1 + 腳本 2 |
| CrashLoopBackOff，exitCode=1 | MariaDB 無法啟動 | §3.2：`logs --previous` |
| exitCode=137，reason=Error（非 OOM），events 有 probe failed | Liveness probe 失敗 | §3.3 |
| exitCode=137，node 有 MemoryPressure | Node eviction | §3.5 |
| exitCode=255，reason=Unknown | Container runtime 異常、node 重啟 | §3.5 |
| logs 有 `InnoDB: Database was not shutdown normally` | 上次非正常關閉，正在 recovery | §4.2：通常自動恢復 |
| replica 的 `SHOW SLAVE STATUS` 有 error | Replication 斷線 | §4.3 + `concepts/11` |
| `kubectl logs --previous` 是空的 | Events 已過期，log 已輪轉 | §5.1：看 operator logs |

### Pod Recreate（AGE 短 + RESTARTS=0）

| 你看到什麼 | 最可能的原因 | 查什麼 |
|-----------|-------------|--------|
| AGE 短，剛改過 MariaDB CR spec | Operator rolling update | §3.6：正常行為 |
| AGE 短，primary 換了（currentPrimary 變了） | Switchover 或 Failover | §3.6 + `concepts/10` |
| AGE 短，primary 換了，`Max_used_connections` ≈ `max_connections` | **max_connections 連鎖反應** | §3.3（連鎖 Failover 段落） |
| AGE 短，node 有 MemoryPressure/DiskPressure | Node drain / eviction | §3.5 |
| AGE 短，operator logs 有 failover 記錄 | 自動 Failover | §3.6 + §五 |
| Pod 卡在 ContainerCreating，events 有 Multi-Attach | PVC 掛載衝突 | §3.4 + `tools/07` |
| AGE 短，找不到任何線索 | 可能有人手動 delete pod | §五：查 operator logs 和 audit log |
