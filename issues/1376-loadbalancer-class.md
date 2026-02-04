# Issue #1376 - Add loadBalancerClass Field

**Status**: PR #1589 Created
**Date**: 2026-01-20

## Problem

Users wanted to integrate with load balancer implementations like Tailscale, but `ServiceTemplate` didn't support the `loadBalancerClass` field.

Kubernetes 1.24+ supports `loadBalancerClass` to specify which load balancer implementation to use.

## What I Learned

### 1. Kubernetes Service LoadBalancer Class

Starting from Kubernetes 1.24, you can specify which load balancer implementation to use:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-service
spec:
  type: LoadBalancer
  loadBalancerClass: "tailscale"  # Use Tailscale instead of cloud provider
  ports:
    - port: 3306
```

This allows multiple load balancer implementations to coexist in a cluster.

### 2. Adding a Field to CRD (3-Step Pattern)

When adding a new field to a Kubernetes Operator CRD:

**Step 1: Define in API types**
```go
// api/v1alpha1/base_types.go
type ServiceTemplate struct {
    // ... existing fields ...

    // LoadBalancerClass Service field.
    // +optional
    // +operator-sdk:csv:customresourcedefinitions:type=spec
    LoadBalancerClass *string `json:"loadBalancerClass,omitempty"`
}
```

**Step 2: Use in Builder**
```go
// pkg/builder/service_builder.go
if opts.LoadBalancerClass != nil {
    svc.Spec.LoadBalancerClass = opts.LoadBalancerClass
}
```

**Step 3: Sync in Reconciler**
```go
// pkg/controller/service/controller.go
existingSvc.Spec.LoadBalancerClass = desiredSvc.Spec.LoadBalancerClass
```

### 3. Kubebuilder Markers

Comments with `+` prefix are kubebuilder markers that control code generation:

| Marker | Purpose |
|--------|---------|
| `+optional` | Field is not required |
| `+operator-sdk:csv:customresourcedefinitions:type=spec` | Include in CSV for OLM |
| `+kubebuilder:validation:Enum=` | Limit allowed values |

### 4. Why Use `*string` Instead of `string`?

```go
LoadBalancerClass *string  // pointer to string
```

Using a pointer allows distinguishing between:
- `nil` - field not set (omit from YAML)
- `""` - field explicitly set to empty string

With `omitempty` JSON tag, `nil` values are omitted from serialization.

### 5. Code Generation Commands

```bash
make code       # Generate DeepCopy methods (zz_generated.deepcopy.go)
make manifests  # Generate CRD YAML files
```

After adding a field, always run both commands to update generated files.

## Files Modified

| File | Changes |
|------|---------|
| `api/v1alpha1/base_types.go` | Added `LoadBalancerClass` field |
| `api/v1alpha1/zz_generated.deepcopy.go` | Auto-generated |
| `config/crd/bases/k8s.mariadb.com_mariadbs.yaml` | Auto-generated |
| `config/crd/bases/k8s.mariadb.com_maxscales.yaml` | Auto-generated |
| `pkg/builder/service_builder.go` | Set field when building Service |
| `pkg/controller/service/controller.go` | Sync field during reconcile |

## Commands Used

```bash
# Generate code
make code
make manifests

# Run lint
make lint

# Git operations
git stash push -m "message"
git checkout main
git checkout -b feat-loadbalancer-class
git stash pop
git add <files>
git commit -m "feat: ..."
git push -u origin feat-loadbalancer-class
```

## Architecture Insight

```
User creates MariaDB CR
        |
        v
MariaDB Controller sees CR
        |
        v
Builder creates desired Service
(uses ServiceTemplate from CR spec)
        |
        v
Service Reconciler compares
desired vs existing Service
        |
        v
Patches differences to cluster
```

The `loadBalancerClass` flows from:
1. User's MariaDB YAML (`spec.service.loadBalancerClass`)
2. To `ServiceTemplate` struct
3. To `ServiceOpts` in Builder
4. To actual `corev1.Service` object
5. Synced by Reconciler on updates

## Code Review Feedback (2026-01-21)

**Maintainer @mmontes11:**
> Thanks for the contribution, changes look good. Could you add a test suite here to validate that the `loadBalancerClass` is effectively added to the `Service` object?
> Additionally, please run `make gen` to generate artifacts and commit the changes.

**Learning:**
- Always add tests when adding new fields - even for simple fields
- Remember to run `make gen` after API changes to generate artifacts
- Tests should follow existing patterns in the codebase (table-driven tests)

**Action Taken:**
- Added `TestServiceLoadBalancerClass` test function in `pkg/builder/service_builder_test.go`
- Ran `make gen` and committed generated files (`deploy/charts/mariadb-operator-crds/templates/crds.yaml`)

### Test Pattern Used

```go
func TestServiceLoadBalancerClass(t *testing.T) {
    builder := newDefaultTestBuilder(t)
    loadBalancerClass := "tailscale"
    tests := []struct {
        name                  string
        opts                  ServiceOpts
        wantLoadBalancerClass *string
    }{
        {
            name: "no loadBalancerClass",
            opts: ServiceOpts{ExcludeSelectorLabels: true},
            wantLoadBalancerClass: nil,
        },
        {
            name: "with loadBalancerClass",
            opts: ServiceOpts{
                ServiceTemplate: mariadbv1alpha1.ServiceTemplate{
                    LoadBalancerClass: &loadBalancerClass,
                },
                ExcludeSelectorLabels: true,
            },
            wantLoadBalancerClass: &loadBalancerClass,
        },
    }
    // ... test logic
}
```
