# MariaDB Kubectl 命令與資源偵測工具

## 一、自動偵測 MariaDB 資源

在多個 StatefulSet 或 MariaDB CR 共存的 namespace 中，需要自動偵測目標資源名稱。

### 偵測 Function

```bash
#!/bin/bash
# detect_mariadb.sh
# 用法：source detect_mariadb.sh && detect_sts <context> <namespace> && detect_mdb <context> <namespace>
# 或直接執行：./detect_mariadb.sh <context> <namespace>
# 結果：設定 STS_NAME 和 MDB_NAME 變數

# 偵測 StatefulSet 名稱
# 0 個：報錯退出
# 1 個：自動使用
# >1 個：列出所有名稱，報錯退出
detect_sts() {
  local ctx="$1" ns="$2"
  local sts_list sts_count

  sts_list=$(kubectl --context="${ctx}" -n "${ns}" get sts --no-headers -o custom-columns=':metadata.name' 2>/dev/null)
  if [ -z "$sts_list" ]; then
    echo "Error: No StatefulSet found in namespace '${ns}'" >&2
    return 1
  fi

  sts_count=$(echo "$sts_list" | wc -l | tr -d ' ')
  if [ "$sts_count" -gt 1 ]; then
    echo "Error: Found ${sts_count} StatefulSets in namespace '${ns}':" >&2
    echo "$sts_list" | while IFS= read -r name; do
      echo "  - ${name}" >&2
    done
    echo "Please specify StatefulSet name as the 3rd argument." >&2
    return 1
  fi

  STS_NAME=$(echo "$sts_list" | head -1)
  echo "Detected StatefulSet: ${STS_NAME}"
}

# 偵測 MariaDB CR 名稱
# 相容不同 CRD API group（community: k8s.mariadb.com, 其他版本可能不同）
# 先試 mariadbs（通用 resource name），失敗再試 mdb（short name）
detect_mdb() {
  local ctx="$1" ns="$2"
  local mdb_list mdb_count

  # 嘗試取得 MariaDB CR
  mdb_list=$(kubectl --context="${ctx}" -n "${ns}" get mariadbs --no-headers -o custom-columns=':metadata.name' 2>/dev/null)
  if [ -z "$mdb_list" ]; then
    # fallback: 嘗試 short name
    mdb_list=$(kubectl --context="${ctx}" -n "${ns}" get mdb --no-headers -o custom-columns=':metadata.name' 2>/dev/null)
  fi

  if [ -z "$mdb_list" ]; then
    echo "Error: No MariaDB CR found in namespace '${ns}'" >&2
    echo "  (tried 'kubectl get mariadbs' and 'kubectl get mdb')" >&2
    return 1
  fi

  mdb_count=$(echo "$mdb_list" | wc -l | tr -d ' ')
  if [ "$mdb_count" -gt 1 ]; then
    echo "Error: Found ${mdb_count} MariaDB CRs in namespace '${ns}':" >&2
    echo "$mdb_list" | while IFS= read -r name; do
      echo "  - ${name}" >&2
    done
    echo "Please specify MariaDB CR name explicitly." >&2
    return 1
  fi

  MDB_NAME=$(echo "$mdb_list" | head -1)
  echo "Detected MariaDB CR: ${MDB_NAME}"
}

# 如果直接執行（非 source），自動偵測並輸出
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  CONTEXT="$1"
  NAMESPACE="$2"

  if [ -z "$NAMESPACE" ]; then
    echo "Usage: $0 <context> <namespace>"
    exit 1
  fi

  detect_sts "$CONTEXT" "$NAMESPACE" || exit 1
  detect_mdb "$CONTEXT" "$NAMESPACE" || exit 1

  echo ""
  echo "=== Results ==="
  echo "STS_NAME=${STS_NAME}"
  echo "MDB_NAME=${MDB_NAME}"

  # 額外資訊
  REPLICAS=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get sts "${STS_NAME}" \
    -o jsonpath='{.spec.replicas}' 2>/dev/null)
  PRIMARY=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get mariadbs "${MDB_NAME}" \
    -o jsonpath='{.status.currentPrimary}' 2>/dev/null || \
    kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get mdb "${MDB_NAME}" \
    -o jsonpath='{.status.currentPrimary}' 2>/dev/null)

  echo "REPLICAS=${REPLICAS}"
  echo "PRIMARY=${PRIMARY}"
fi
```

### 使用方式

```bash
# 方式 1：直接執行
./detect_mariadb.sh kind-mdb default

# 方式 2：在其他腳本中 source
source detect_mariadb.sh
detect_sts "kind-mdb" "default"
detect_mdb "kind-mdb" "default"
echo "StatefulSet: $STS_NAME, MariaDB CR: $MDB_NAME"
```

### 輸出範例

**正常（各 1 個）**：

```
Detected StatefulSet: mariadb-chaos
Detected MariaDB CR: mariadb-chaos

=== Results ===
STS_NAME=mariadb-chaos
MDB_NAME=mariadb-chaos
REPLICAS=3
PRIMARY=mariadb-chaos-1
```

**多個 StatefulSet**：

```
Error: Found 3 StatefulSets in namespace 'default':
  - csi-hostpath-socat
  - csi-hostpathplugin
  - mariadb-chaos
Please specify StatefulSet name as the 3rd argument.
```

---

## 二、常用 kubectl 命令速查

以下命令全部帶 `--context` 和 `-n`，直接複製貼上即可使用。

將 `CTX`、`NS`、`STS`、`MDB` 替換成你的值：

```bash
CTX=kind-mdb
NS=default
STS=mariadb-chaos
MDB=mariadb-chaos
```

### 資源總覽

```bash
# StatefulSet + MariaDB CR + Pods 一次看
kubectl --context=$CTX -n $NS get sts,mdb,pods -o wide

# 只看 MariaDB 相關 pods
kubectl --context=$CTX -n $NS get pods -l app.kubernetes.io/instance=$STS -o wide

# MariaDB CRD 的所有 short names
kubectl --context=$CTX api-resources | grep -i mariadb
```

### Pod 狀態

```bash
# Pod 詳情（Events、container states、restarts）
kubectl --context=$CTX -n $NS describe pod ${STS}-0

# 所有 container 的 restart 資訊
kubectl --context=$CTX -n $NS get pod ${STS}-0 \
  -o jsonpath='{range .status.containerStatuses[*]}{.name}{"\t restarts="}{.restartCount}{"\t reason="}{.lastState.terminated.reason}{"\n"}{end}'

# Pod UID 和建立時間
kubectl --context=$CTX -n $NS get pod ${STS}-0 \
  -o jsonpath='uid={.metadata.uid} created={.metadata.creationTimestamp}'
```

### Logs

```bash
# 目前的 mariadb container logs
kubectl --context=$CTX -n $NS logs ${STS}-0 -c mariadb --tail=100

# 上一次 crash 的 logs（最重要！）
kubectl --context=$CTX -n $NS logs ${STS}-0 -c mariadb --previous --tail=100

# Agent sidecar logs（probe 相關）
kubectl --context=$CTX -n $NS logs ${STS}-0 -c agent --tail=50

# Operator logs
OPERATOR_POD=$(kubectl --context=$CTX -n mariadb-operator-system get pods \
  -l control-plane=mariadb-operator-controller-manager -o name | head -1)
kubectl --context=$CTX -n mariadb-operator-system logs "$OPERATOR_POD" --since=1h | grep "$STS"
```

### MariaDB CR Status

```bash
# 完整 status
kubectl --context=$CTX -n $NS get mdb $MDB -o yaml | grep -A 50 "^status:"

# Conditions 一覽
kubectl --context=$CTX -n $NS get mdb $MDB \
  -o jsonpath='{range .status.conditions[*]}{.type}{"\t"}{.status}{"\t"}{.message}{"\n"}{end}'

# Current primary
kubectl --context=$CTX -n $NS get mdb $MDB -o jsonpath='{.status.currentPrimary}'
```

### Events

```bash
# 特定 pod 的 events
kubectl --context=$CTX -n $NS get events \
  --field-selector involvedObject.name=${STS}-0 \
  --sort-by='.lastTimestamp'

# 所有 Warning events
kubectl --context=$CTX -n $NS get events \
  --field-selector type=Warning \
  --sort-by='.lastTimestamp'

# Failover / Switchover events
kubectl --context=$CTX -n $NS get events \
  --field-selector reason=Failover --sort-by='.lastTimestamp' 2>/dev/null
kubectl --context=$CTX -n $NS get events \
  --field-selector reason=Switchover --sort-by='.lastTimestamp' 2>/dev/null
```

### MariaDB SQL（透過 kubectl exec）

```bash
# 取得密碼
MYSQL_PWD=$(kubectl --context=$CTX -n $NS exec ${STS}-0 -c mariadb -- printenv MARIADB_ROOT_PASSWORD)

# 執行 SQL
kubectl --context=$CTX -n $NS exec ${STS}-0 -c mariadb -- \
  mariadb -u root -p"${MYSQL_PWD}" -e "SELECT @@version, @@uptime, @@max_connections" 2>/dev/null

# Replication 狀態
kubectl --context=$CTX -n $NS exec ${STS}-0 -c mariadb -- \
  mariadb -u root -p"${MYSQL_PWD}" -e "SHOW SLAVE STATUS\G" 2>/dev/null

# max_connections 使用率
kubectl --context=$CTX -n $NS exec ${STS}-0 -c mariadb -- \
  mariadb -u root -p"${MYSQL_PWD}" -e "
    SELECT @@max_connections AS max_conn,
           (SELECT VARIABLE_VALUE FROM information_schema.GLOBAL_STATUS
            WHERE VARIABLE_NAME='Max_used_connections') AS max_used,
           (SELECT VARIABLE_VALUE FROM information_schema.GLOBAL_STATUS
            WHERE VARIABLE_NAME='Threads_connected') AS current_conn
  " 2>/dev/null
```

### Node 檢查

```bash
# Pod 所在 node
NODE=$(kubectl --context=$CTX -n $NS get pod ${STS}-0 -o jsonpath='{.spec.nodeName}')
echo "Node: $NODE"

# Node conditions
kubectl --context=$CTX describe node "$NODE" | grep -E "Conditions:|MemoryPressure|DiskPressure|PIDPressure"

# Node 資源使用
kubectl --context=$CTX top node "$NODE"
```

### Probe 檢查

```bash
# Liveness probe 設定
kubectl --context=$CTX -n $NS get pod ${STS}-0 \
  -o jsonpath='{.spec.containers[0].livenessProbe}' | python3 -m json.tool

# 手動測試 probe endpoint
kubectl --context=$CTX -n $NS port-forward ${STS}-0 5566:5566 &
curl -s http://localhost:5566/liveness ; echo
curl -s http://localhost:5566/readiness ; echo
kill %1 2>/dev/null
```
