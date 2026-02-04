# MariaDB Operator Failover 流程

## 什麼是 Failover？

Failover 是**非計畫性**的 primary 切換，當 primary pod 無法運作時自動觸發。

| | Switchover | Failover |
|---|------------|----------|
| 觸發 | 手動修改 spec | 自動偵測 |
| 舊 Primary | 仍在運作 | 已經掛掉 |
| 資料同步 | 可以等待 | 無法等待 |
| 資料遺失 | 無 | 可能有（async replication） |

## 觸發條件

Primary Pod 變成 **NotReady** 狀態：

```go
// pkg/controller/replication/replication.go
func (r *ReplicationReconciler) shouldFailover(mariadb *v1alpha1.MariaDB) bool {
    primaryPod := getPrimaryPod(mariadb)

    // 檢查 Pod 是否 Ready
    if !isPodReady(primaryPod) {
        return true
    }

    return false
}
```

### Pod NotReady 的情況

1. **Container crash**
   - MariaDB process 掛掉
   - OOM killed

2. **Readiness probe 失敗**
   - MariaDB 無法接受連線
   - Health check query 失敗

3. **Node 問題**
   - Node NotReady
   - Network partition

### Readiness Probe 檢查內容

```yaml
# mariadb StatefulSet
readinessProbe:
  exec:
    command:
      - /bin/sh
      - -c
      - mysql -u root -p$MYSQL_ROOT_PASSWORD -e "SELECT 1"
  initialDelaySeconds: 10
  periodSeconds: 5
  failureThreshold: 3
```

連續 3 次失敗（15 秒）後，Pod 變成 NotReady。

## automaticFailoverDelay 延遲機制

為了避免誤判（例如短暫的網路問題），operator 有延遲機制：

```yaml
apiVersion: k8s.mariadb.com/v1alpha1
kind: MariaDB
spec:
  replication:
    enabled: true
    replica:
      automaticFailoverDelay: 30s  # 預設 30 秒
```

```go
// pkg/controller/replication/failover.go
func (r *ReplicationReconciler) handleFailover(ctx context.Context, mariadb *v1alpha1.MariaDB) error {
    if !isPrimaryReady(mariadb) {
        // 記錄首次偵測到 NotReady 的時間
        if mariadb.Status.PrimaryNotReadySince == nil {
            mariadb.Status.PrimaryNotReadySince = &metav1.Time{Time: time.Now()}
            return nil  // 等待下次 reconcile
        }

        // 檢查是否超過延遲時間
        elapsed := time.Since(mariadb.Status.PrimaryNotReadySince.Time)
        if elapsed < mariadb.Spec.Replication.Replica.AutomaticFailoverDelay {
            return nil  // 還在等待
        }

        // 執行 failover
        return r.doFailover(ctx, mariadb)
    }

    return nil
}
```

時序：
```
T+0s:   Primary NotReady 偵測
T+5s:   Reconcile - 記錄 PrimaryNotReadySince
T+10s:  Reconcile - 等待中 (10s < 30s)
T+20s:  Reconcile - 等待中 (20s < 30s)
T+30s:  Reconcile - 執行 failover (30s >= 30s)
```

## FurthestAdvancedReplica() 演算法

選擇新 primary 的核心演算法：

```go
// pkg/controller/replication/failover.go
func (r *ReplicationReconciler) FurthestAdvancedReplica(
    ctx context.Context,
    mariadb *v1alpha1.MariaDB,
    replicas []*corev1.Pod,
) (*corev1.Pod, error) {
    var candidates []*replicaCandidate

    for _, pod := range replicas {
        // 1. 篩選候選人
        if !isValidCandidate(pod) {
            continue
        }

        // 2. 取得 GTID
        client := r.getSqlClient(pod)
        gtidCurrentPos, _ := client.GetVariable(ctx, "gtid_current_pos")

        candidates = append(candidates, &replicaCandidate{
            pod:  pod,
            gtid: gtidCurrentPos,
        })
    }

    // 3. 比較 GTID，找出最新的
    sort.Slice(candidates, func(i, j int) bool {
        return compareGTID(candidates[i].gtid, candidates[j].gtid) > 0
    })

    if len(candidates) == 0 {
        return nil, errors.New("no valid candidate found")
    }

    return candidates[0].pod, nil
}
```

## 候選人篩選條件

```go
func isValidCandidate(pod *corev1.Pod) bool {
    // 1. Pod 必須 Ready
    if !isPodReady(pod) {
        return false
    }

    // 2. 不能是目前的 primary（因為它掛了）
    if isPrimary(pod) {
        return false
    }

    // 3. Replication 必須正常
    slaveStatus := getSlaveStatus(pod)
    if slaveStatus.SlaveIORunning != "Yes" || slaveStatus.SlaveSQLRunning != "Yes" {
        return false
    }

    return true
}
```

### 篩選條件詳解

| 條件 | 原因 |
|------|------|
| Pod Ready | 確保可以接受連線 |
| 非 Primary | Primary 已經掛了 |
| IO Thread Running | 確保還在接收資料 |
| SQL Thread Running | 確保還在執行 transaction |

## GTID 比較邏輯

```go
// pkg/controller/replication/replication.go
func compareGTID(a, b string) int {
    // 解析 GTID: "0-10-100,1-11-50"
    gtidsA := parseGTID(a)
    gtidsB := parseGTID(b)

    // 比較每個 domain
    for domain, seqA := range gtidsA {
        seqB := gtidsB[domain]

        if seqA > seqB {
            return 1   // a 比較新
        } else if seqA < seqB {
            return -1  // b 比較新
        }
    }

    return 0  // 相同
}

func parseGTID(gtidStr string) map[int]int64 {
    // "0-10-100" → {0: 100}
    // "0-10-100,1-11-50" → {0: 100, 1: 50}
    result := make(map[int]int64)

    for _, gtid := range strings.Split(gtidStr, ",") {
        parts := strings.Split(gtid, "-")
        domain, _ := strconv.Atoi(parts[0])
        seq, _ := strconv.ParseInt(parts[2], 10, 64)
        result[domain] = seq
    }

    return result
}
```

### 比較範例

```
Replica A: gtid_current_pos = 0-10-100
Replica B: gtid_current_pos = 0-10-105
Replica C: gtid_current_pos = 0-10-98

比較結果：B > A > C
選擇 Replica B 作為新 Primary
```

## Failover 執行流程

```go
func (r *ReplicationReconciler) doFailover(ctx context.Context, mariadb *v1alpha1.MariaDB) error {
    // 1. 找出所有 ready 的 replica
    replicas := getReadyReplicas(mariadb)

    // 2. 選出最新的 replica
    newPrimary, err := r.FurthestAdvancedReplica(ctx, mariadb, replicas)
    if err != nil {
        return err
    }

    // 3. 設定為 primary
    client := r.getSqlClient(newPrimary)
    client.StopAllSlaves(ctx)
    client.ResetAllSlaves(ctx)
    client.ResetMaster(ctx)
    client.SetReadOnly(ctx, false)

    // 4. 設定其他 replica 指向新 primary
    for _, replica := range replicas {
        if replica.Name == newPrimary.Name {
            continue
        }
        r.configureReplica(ctx, replica, newPrimary)
    }

    // 5. 更新 status
    mariadb.Status.CurrentPrimaryPodIndex = getPodIndex(newPrimary)

    return nil
}
```

## Corner Cases

### 1. 所有 GTID 相同

當所有 replica 的 GTID 都相同時：

```go
// 選擇 pod index 最小的
sort.Slice(candidates, func(i, j int) bool {
    if compareGTID(candidates[i].gtid, candidates[j].gtid) == 0 {
        return getPodIndex(candidates[i].pod) < getPodIndex(candidates[j].pod)
    }
    return compareGTID(candidates[i].gtid, candidates[j].gtid) > 0
})
```

結果：選擇 `mariadb-0`（如果可用）或下一個最小 index。

### 2. 沒有合格候選人

```go
if len(candidates) == 0 {
    // 記錄事件
    r.recorder.Event(mariadb, "Warning", "FailoverFailed",
        "No valid candidate for failover")

    // 不執行 failover，等待人工介入
    return nil
}
```

可能原因：
- 所有 replica 都 NotReady
- 所有 replica 的 replication 都斷了
- 只有 1 個 pod（沒有 replica）

### 3. Relay Log Pending

Replica 可能已經接收但尚未執行某些 transaction：

```
IO Thread: 已接收 GTID 0-10-105
SQL Thread: 已執行 GTID 0-10-100
Pending: 5 個 transaction
```

目前 operator 使用 `gtid_current_pos`（已執行的），不考慮 pending。

這是有意的設計：
- Pending 的 transaction 可能有問題
- 已執行的比較可靠
- 避免選到「看起來最新」但實際有問題的 replica

### 4. Split Brain 風險

如果舊 primary 其實沒有真的掛，只是網路問題：

```
Network partition:
  Pod-0 (old primary): 仍在運作，接受 writes
  Pod-1, Pod-2: 看不到 Pod-0，執行 failover

結果: 兩個 primary 同時存在！
```

預防機制：
1. `automaticFailoverDelay` 等待期
2. Fencing：確保舊 primary 無法寫入
3. Semi-sync replication：減少資料不一致

## 相關程式碼位置

| 功能 | 檔案 |
|------|------|
| Failover 主流程 | `pkg/controller/replication/failover.go` |
| 候選人選擇 | `pkg/controller/replication/failover.go:FurthestAdvancedReplica()` |
| GTID 比較 | `pkg/controller/replication/replication.go` |
| 狀態檢查 | `pkg/controller/replication/replication.go` |

## 監控 Failover

### 檢查 Failover 狀態

```bash
# 查看 events
kubectl get events --field-selector reason=Failover

# 查看 operator logs
kubectl logs deployment/mariadb-operator-controller-manager | grep -i failover

# 查看目前 primary
kubectl get mariadb mariadb -o jsonpath='{.status.currentPrimaryPodIndex}'
```

### Metrics

```
# Failover 次數
mariadb_failover_total

# Failover 延遲
mariadb_failover_duration_seconds
```

## 常見問題

### Q: Failover 後舊 primary 恢復了怎麼辦？

Operator 會自動將它設定為 replica：
1. 偵測到舊 primary pod Ready
2. 發現它不是目前的 primary
3. 執行 `ConfigureReplica()`，指向新 primary

### Q: 如何手動觸發 failover？

刪除 primary pod：
```bash
kubectl delete pod mariadb-0
```

或修改 `podIndex`（這是 switchover，不是 failover）。

### Q: Failover 會遺失資料嗎？

使用 async replication 時可能會：
- Primary 最後幾個 transaction 可能還沒傳到 replica
- 這些 transaction 會遺失

使用 semi-sync replication 可以減少風險：
- Primary 等待至少 1 個 replica 確認
- 但如果 timeout，仍會降級為 async

### Q: 如何避免誤觸發 failover？

1. 增加 `automaticFailoverDelay`
2. 確保 readiness probe 設定合理
3. 使用 PodDisruptionBudget 限制同時中斷的 pod 數量
