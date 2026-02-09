# No-Index Query 監控腳本

## 腳本 1：用 Digest 查完整 SQL

```bash
#!/bin/bash
# get_sql_by_digest.sh
# 用法：./get_sql_by_digest.sh <context> <namespace> <statefulset> <digest> [--verbose]

CONTEXT="$1"
NAMESPACE="$2"
STS_NAME="$3"
DIGEST="$4"
VERBOSE="${5:-}"

if [ -z "$DIGEST" ]; then
  echo "Usage: $0 <context> <namespace> <statefulset> <digest> [--verbose]"
  exit 1
fi

PODS=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pods \
  -l app.kubernetes.io/instance="${STS_NAME}" \
  -o jsonpath='{.items[*].metadata.name}')

echo "Digest: ${DIGEST}"
echo ""

FOUND=0

for POD in $PODS; do
  if [ "$VERBOSE" = "--verbose" ]; then
    echo "----------------------------------------"
    echo "Pod: ${POD}"

    ROLE=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
      mysql -N -e "SELECT IF(@@read_only = 0, 'PRIMARY', 'REPLICA')" 2>/dev/null)
    echo "Role: ${ROLE}"

    RESULT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
      mysql -N -e "
        SELECT CONCAT(
          'Schema: ', IFNULL(CURRENT_SCHEMA, 'N/A'), '\n',
          'SQL: ', SQL_TEXT, '\n',
          'Rows_examined: ', ROWS_EXAMINED, '\n',
          'Rows_sent: ', ROWS_SENT
        )
        FROM performance_schema.events_statements_history
        WHERE DIGEST = '${DIGEST}'
        ORDER BY EVENT_ID DESC
        LIMIT 2
      " 2>/dev/null)

    if [ -n "$RESULT" ]; then
      echo "[Found]"
      echo "$RESULT"
      FOUND=1
    else
      echo "[Not found in history]"
    fi
    echo ""
  else
    RESULT=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
      mysql -N -e "
        SELECT SQL_TEXT
        FROM performance_schema.events_statements_history
        WHERE DIGEST = '${DIGEST}'
        LIMIT 1
      " 2>/dev/null)

    if [ -n "$RESULT" ]; then
      ROLE=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
        mysql -N -e "SELECT IF(@@read_only = 0, 'P', 'R')" 2>/dev/null)
      echo "[${POD}] (${ROLE}) ${RESULT}"
      FOUND=1
    fi
  fi
done

if [ "$FOUND" -eq 0 ] || [ "$VERBOSE" = "--verbose" ]; then
  echo ""
  echo "=== Digest Pattern (from summary) ==="
  FIRST_POD=$(echo $PODS | awk '{print $1}')
  kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${FIRST_POD}" -c mariadb -- \
    mysql -N -e "
      SELECT CONCAT(
        'Schema: ', IFNULL(SCHEMA_NAME, 'N/A'), '\n',
        'Pattern: ', DIGEST_TEXT, '\n',
        'No_index_count: ', SUM_NO_INDEX_USED, '\n',
        'Total_exec: ', COUNT_STAR
      )
      FROM performance_schema.events_statements_summary_by_digest
      WHERE DIGEST = '${DIGEST}'
    " 2>/dev/null
fi
```

### 輸出範例

**精簡版（預設）**：
```
Digest: abc123def456...

[mariadb-0] (P) SELECT * FROM users WHERE email = 'test@example.com'
[mariadb-2] (R) SELECT * FROM users WHERE email = 'foo@bar.com'

=== Digest Pattern (from summary) ===
Schema: myapp
Pattern: SELECT * FROM users WHERE email = ?
No_index_count: 1000
Total_exec: 5000
```

**完整版（--verbose）**：
```
Digest: abc123def456...

----------------------------------------
Pod: mariadb-0
Role: PRIMARY
[Found]
Schema: myapp
SQL: SELECT * FROM users WHERE email = 'test@example.com'
Rows_examined: 50000
Rows_sent: 1

----------------------------------------
Pod: mariadb-1
Role: REPLICA
[Not found in history]

----------------------------------------
Pod: mariadb-2
Role: REPLICA
[Found]
Schema: myapp
SQL: SELECT * FROM users WHERE email = 'foo@bar.com'
Rows_examined: 50000
Rows_sent: 1
```

---

## 腳本 2：分析 SQL

```bash
#!/bin/bash
# analyze_sql.sh
# 用法：./analyze_sql.sh <context> <namespace> <statefulset> "<SQL>"
# 自動找到 Primary pod 執行 EXPLAIN

CONTEXT="$1"
NAMESPACE="$2"
STS_NAME="$3"
SQL="$4"

if [ -z "$SQL" ]; then
  echo "Usage: $0 <context> <namespace> <statefulset> \"<SQL>\""
  exit 1
fi

# 找出所有 pods
PODS=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" get pods \
  -l app.kubernetes.io/instance="${STS_NAME}" \
  -o jsonpath='{.items[*].metadata.name}')

if [ -z "$PODS" ]; then
  echo "Error: No pods found for StatefulSet ${STS_NAME}"
  exit 1
fi

# 找出 Primary pod（@@read_only = 0）
PRIMARY_POD=""
for POD in $PODS; do
  READ_ONLY=$(kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${POD}" -c mariadb -- \
    mysql -N -e "SELECT @@read_only" 2>/dev/null)

  if [ "$READ_ONLY" = "0" ]; then
    PRIMARY_POD="$POD"
    break
  fi
done

if [ -z "$PRIMARY_POD" ]; then
  echo "Error: No Primary pod found (all pods are read_only)"
  echo "Available pods: $PODS"
  exit 1
fi

echo "=== Target ==="
echo "StatefulSet: ${STS_NAME}"
echo "Primary Pod: ${PRIMARY_POD}"
echo ""

echo "=== SQL ==="
echo "$SQL"
echo ""

echo "=== EXPLAIN ==="
kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${PRIMARY_POD}" -c mariadb -- \
  mysql -e "EXPLAIN ${SQL}" 2>/dev/null

echo ""
echo "=== 解讀 ==="
kubectl --context="${CONTEXT}" -n "${NAMESPACE}" exec "${PRIMARY_POD}" -c mariadb -- \
  mysql -e "
    SELECT
      CASE
        WHEN type = 'ALL' THEN '!! Full Table Scan'
        WHEN type = 'index' THEN '! Full Index Scan'
        WHEN type IN ('ref', 'eq_ref', 'range', 'const') THEN 'OK - Using Index'
        ELSE CONCAT('type=', type)
      END AS diagnosis,
      IFNULL(possible_keys, 'none') AS possible_indexes,
      IFNULL(key, 'none') AS actual_index,
      rows AS estimated_rows
    FROM (EXPLAIN ${SQL}) AS e
  " 2>/dev/null

echo ""
echo "=== 檢查清單 ==="
echo "[ ] type=ALL? 需要加 Index"
echo "[ ] possible_keys 有值但 key 是 NULL? Optimizer 選擇不用（可能正常）"
echo "[ ] WHERE 條件有 Function? 例如 YEAR(date)"
echo "[ ] 有隱式型別轉換? VARCHAR vs INT"
echo "[ ] LIKE 開頭是 %?"
echo "[ ] 統計資訊過時? 試試 ANALYZE TABLE"
```

### 輸出範例

**沒用 Index（有問題）**：
```
=== Target ===
StatefulSet: mariadb
Primary Pod: mariadb-0

=== SQL ===
SELECT * FROM users WHERE email = 'test@test.com'

=== EXPLAIN ===
+----+-------------+-------+------+---------------+------+---------+------+--------+-------------+
| id | select_type | table | type | possible_keys | key  | key_len | ref  | rows   | Extra       |
+----+-------------+-------+------+---------------+------+---------+------+--------+-------------+
|  1 | SIMPLE      | users | ALL  | NULL          | NULL | NULL    | NULL | 100000 | Using where |
+----+-------------+-------+------+---------------+------+---------+------+--------+-------------+

=== 解讀 ===
+----------------------+------------------+--------------+----------------+
| diagnosis            | possible_indexes | actual_index | estimated_rows |
+----------------------+------------------+--------------+----------------+
| !! Full Table Scan   | none             | none         | 100000         |
+----------------------+------------------+--------------+----------------+

=== 檢查清單 ===
[ ] type=ALL? 需要加 Index
...
```

**有用 Index（正常）**：
```
=== Target ===
StatefulSet: mariadb
Primary Pod: mariadb-1

=== EXPLAIN ===
+----+-------------+-------+------+---------------+-----------+---------+-------+------+-------+
| id | select_type | table | type | possible_keys | key       | key_len | ref   | rows | Extra |
+----+-------------+-------+------+---------------+-----------+---------+-------+------+-------+
|  1 | SIMPLE      | users | ref  | idx_email     | idx_email | 767     | const |    1 |       |
+----+-------------+-------+------+---------------+-----------+---------+-------+------+-------+

=== 解讀 ===
+------------------+------------------+--------------+----------------+
| diagnosis        | possible_indexes | actual_index | estimated_rows |
+------------------+------------------+--------------+----------------+
| OK - Using Index | idx_email        | idx_email    | 1              |
+------------------+------------------+--------------+----------------+
```

---

## 使用流程

```
Step 1: 從 Grafana 拿到 digest
        │
        ▼
Step 2: ./get_sql_by_digest.sh prod mariadb mariadb <digest>
        └── 得到完整 SQL
        │
        ▼
Step 3: ./analyze_sql.sh prod mariadb mariadb "<SQL>"
        └── 自動找 Primary pod 執行 EXPLAIN
        │
        ├── type=ALL, possible_keys=none → 需要加 index
        ├── type=ALL, possible_keys 有值 → Optimizer 選擇不用，可能正常
        └── type=ref/range → 已經有用 index
```

---

## Rundeck Job 設定

### Job 1: Get SQL by Digest

```yaml
name: Get SQL by Digest
description: 用 digest 查詢完整 SQL（查所有 pods）
options:
  - name: CONTEXT
    description: Kubernetes context
    required: true
  - name: NAMESPACE
    description: MariaDB namespace
    required: true
    default: mariadb
  - name: STATEFULSET
    description: StatefulSet name
    required: true
    default: mariadb
  - name: DIGEST
    description: SQL digest from Prometheus
    required: true
  - name: VERBOSE
    description: Show detailed output
    values: ["", "--verbose"]
sequence:
  commands:
    - script: |
        /path/to/get_sql_by_digest.sh \
          "@option.CONTEXT@" \
          "@option.NAMESPACE@" \
          "@option.STATEFULSET@" \
          "@option.DIGEST@" \
          @option.VERBOSE@
```

### Job 2: Analyze SQL

```yaml
name: Analyze SQL
description: 分析 SQL 的執行計畫（自動選擇 Primary pod）
options:
  - name: CONTEXT
    description: Kubernetes context
    required: true
  - name: NAMESPACE
    description: MariaDB namespace
    required: true
    default: mariadb
  - name: STATEFULSET
    description: StatefulSet name
    required: true
    default: mariadb
  - name: SQL
    description: SQL to analyze
    required: true
sequence:
  commands:
    - script: |
        /path/to/analyze_sql.sh \
          "@option.CONTEXT@" \
          "@option.NAMESPACE@" \
          "@option.STATEFULSET@" \
          "@option.SQL@"
```
