# Pod Restart 診斷指南

## 背景

MariaDB pod 在 Kubernetes 中重啟，可能是 OOM、應用 crash、probe 失敗、storage 問題、node 問題，或 operator 主動觸發。
本指南提供系統性的診斷流程，從「發現 restartCount > 0」開始，逐步定位根因。

**mariadb-operator 環境的特殊性**：
- StatefulSet 管理 pod — pod 名稱固定（`<sts>-0`, `<sts>-1`, ...），PVC 不會因重啟而改變
- Operator 會主動觸發 pod 重啟（rolling update、switchover、failover）
- Liveness/readiness probe 由 operator 設定，失敗時會導致 container 被 kill 或 pod 從 service 移除

---

## 診斷流程圖

```
restartCount > 0
│
├─ kubectl describe pod → lastState.terminated
│   │
│   ├─ reason=OOMKilled, exitCode=137
│   │   └→ §3.1：記憶體不足（InnoDB buffer pool、查詢暴衝）
│   │
│   ├─ reason=Error, exitCode=1
│   │   └→ §3.2：MariaDB crash（config 錯誤、InnoDB corruption）
│   │
│   ├─ reason=Error, exitCode=137（非 OOMKilled）
│   │   └→ §3.3 或 §3.5：Liveness probe 失敗 或 node eviction（SIGKILL）
│   │
│   ├─ reason=Error, exitCode=139
│   │   └→ §3.2：SIGSEGV（MariaDB bug 或記憶體損壞）
│   │
│   ├─ reason=Error, exitCode=143
│   │   └→ §4.1：SIGTERM 未正常處理（graceful shutdown 失敗）
│   │
│   ├─ reason=Completed, exitCode=0
│   │   └→ §3.6：正常退出後被重啟（operator rolling update）
│   │
│   ├─ reason=Unknown, exitCode=255
│   │   └→ §3.5：Container runtime 異常、node 重啟、kubelet restart
│   │
│   └─ 無 lastState（pod 剛建立或 events 已過期）
│       └→ 檢查 events → 見下方
│
├─ kubectl get events --field-selector
│   │
│   ├─ "Liveness probe failed"
│   │   └→ §3.3：Liveness Probe 失敗
│   │
│   ├─ "Evicted" / "The node was low on resource"
│   │   └→ §3.5：Node 資源不足
│   │
│   ├─ "Multi-Attach error for volume"
│   │   └→ §3.4：Storage 問題（參考 tools/07）
│   │
│   └─ "Killing" / "Stopping container"
│       └→ §3.6：Operator 或使用者觸發的重啟
│
└─ kubectl logs --previous
    └→ §4：MariaDB error log 分析
```

---

## 一、快速分類（Triage）

### 1.1 檢查重啟次數與最近終止原因

```bash
# 列出所有 pod 的 RESTARTS 次數
kubectl --context=kind-mdb -n default get pods -l app.kubernetes.io/instance=mariadb-chaos -o wide

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

## 七、常見場景速查表

| 你看到什麼 | 最可能的原因 | 查什麼 |
|-----------|-------------|--------|
| restartCount 持續增加，reason=OOMKilled | 記憶體不足 | §3.1 + 腳本 2 |
| CrashLoopBackOff，exitCode=1 | MariaDB 無法啟動 | §3.2：`logs --previous` |
| restartCount=1，exitCode=0，剛改過 CR spec | Operator rolling update | §3.6：正常行為 |
| restartCount 在 failover 後增加 | Operator failover 重建 | §3.6 + `concepts/10` |
| exitCode=137，reason=Error（非 OOM），events 有 probe failed | Liveness probe 失敗 | §3.3 |
| Pod 卡在 ContainerCreating，events 有 Multi-Attach | PVC 掛載衝突 | §3.4 + `tools/07` |
| exitCode=137，node 有 MemoryPressure | Node eviction | §3.5 |
| logs 有 `InnoDB: Database was not shutdown normally` | 上次非正常關閉，正在 recovery | §4.2：通常自動恢復 |
| replica 的 `SHOW SLAVE STATUS` 有 error | Replication 斷線 | §4.3 + `concepts/11` |
| `kubectl logs --previous` 是空的 | Events 已過期，log 已輪轉 | §5.1：看 operator logs |
