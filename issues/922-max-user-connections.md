# Issue #922 - maxUserConnections nullable

**Status**: PR #1595 Created
**Date**: 2026-01-22
**Branch**: `feat-max-user-connections-nullable`

## Problem

The `maxUserConnections` field in User CR was hardcoded to default to `10`. Users couldn't choose to use the MariaDB server's default setting.

```go
// Before: int32 with default 10
// +kubebuilder:default=10
MaxUserConnections int32 `json:"maxUserConnections,omitempty"`
```

| YAML | Result |
|------|--------|
| Not set | Defaults to 10 |
| `0` | Treated as "not set" (Go zero value) |

## Solution

Change to `*int32` (pointer) to distinguish between "not set" and "explicitly set to 0":

```go
// After: *int32 without default
MaxUserConnections *int32 `json:"maxUserConnections,omitempty"`
```

| YAML | Go Value | SQL Generated |
|------|----------|---------------|
| Not set | `nil` | No `WITH MAX_USER_CONNECTIONS` clause |
| `0` | `&0` | `WITH MAX_USER_CONNECTIONS 0` (unlimited) |
| `20` | `&20` | `WITH MAX_USER_CONNECTIONS 20` |

## What I Learned

### 1. Three-Value Logic with Pointers

Go's zero values make it impossible to distinguish "not set" from "set to zero":

```go
var x int32    // x = 0
x = 0          // x = 0  (same!)

var y *int32   // y = nil (not set)
y = ptr.To(0)  // y = &0  (explicitly set to 0)
```

### 2. Kubebuilder Marker Removal

Pointer types don't support `+kubebuilder:default`:

```go
// This doesn't work with *int32:
// +kubebuilder:default=10

// Must handle defaults in code instead
```

### 3. SQL Query Conditional Generation

Before:
```go
query += fmt.Sprintf("WITH MAX_USER_CONNECTIONS %d ", opts.MaxUserConnections)
```

After:
```go
if opts.MaxUserConnections != nil {
    query += fmt.Sprintf("WITH MAX_USER_CONNECTIONS %d ", *opts.MaxUserConnections)
}
```

### 4. Using `ptr.To()` in Tests

```go
// Before
MaxUserConnections: 20,

// After
MaxUserConnections: ptr.To(int32(20)),
```

Import: `"k8s.io/utils/ptr"`

### 5. Cross-File Consistency

This change required updates across many files:

| File Type | Count | Pattern |
|-----------|-------|---------|
| API types | 1 | `int32` -> `*int32` |
| SQL/Builder | 2 | Struct + nil checks |
| Controllers | 3 | `ptr.To()` wrapping |
| Tests | 6 | `ptr.To()` for values |

## Files Modified

| File | Changes |
|------|---------|
| `api/v1alpha1/user_types.go` | `int32` -> `*int32`, removed default |
| `pkg/sql/sql.go` | CreateUserOpts, nil checks in queries |
| `pkg/builder/sql_builder.go` | UserOpts struct |
| `internal/controller/mariadb_controller.go` | `ptr.To()` |
| `internal/controller/mariadb_controller_metrics.go` | Added ptr import, `ptr.To()` |
| `internal/controller/maxscale_controller.go` | `ptr.To()` for int32 fields |
| `internal/controller/user_controller_test.go` | `ptr.To()` |
| `internal/controller/grant_controller_test.go` | Added ptr import, `ptr.To()` |
| `internal/webhook/v1alpha1/user_webhook_test.go` | `ptr.To()` |
| `internal/helmtest/mariadb_cluster_test.go` | Added ptr import, `ptr.To()` |

## Data Flow

```
User YAML                     Controller                    SQL Query
+-----------------+          +-----------------+          +-----------------+
| (not set)       |    ->    | *int32 = nil    |    ->    | CREATE USER ... |
|                 |          |                 |          | (no WITH clause)|
+-----------------+          +-----------------+          +-----------------+

+-----------------+          +-----------------+          +-----------------+
| maxUserConn: 0  |    ->    | *int32 = &0     |    ->    | ... WITH MAX_   |
|                 |          |                 |          | USER_CONN 0     |
+-----------------+          +-----------------+          +-----------------+

+-----------------+          +-----------------+          +-----------------+
| maxUserConn: 20 |    ->    | *int32 = &20    |    ->    | ... WITH MAX_   |
|                 |          |                 |          | USER_CONN 20    |
+-----------------+          +-----------------+          +-----------------+
```

## Commands Used

```bash
# Create branch
git checkout main && git pull origin main
git checkout -b feat-max-user-connections-nullable

# Generate code
make gen

# Run tests
go test ./pkg/sql/... -v
go test ./pkg/builder/... -v

# Lint
make lint
```

## Key Takeaways

1. **Use `*int32` when you need three states**: not set, zero, and positive values
2. **Remove kubebuilder defaults** when changing to pointer types
3. **Add nil checks** before dereferencing pointers
4. **Use `ptr.To()`** utility for creating pointers in Go
5. **Check all usages** across the codebase when changing a type
