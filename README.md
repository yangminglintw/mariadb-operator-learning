# MariaDB Operator Learning Notes

Learning notes and concepts for contributing to the
[mariadb-operator](https://github.com/mariadb-operator/mariadb-operator) project.

## Topics Covered

### Kubernetes Operator Basics
- Operator pattern (CRD + Controller)
- Reconcile loop (desired vs actual state)
- CRD field addition workflow (API → Builder → Reconciler)
- Kubebuilder markers (+optional, +operator-sdk)

### Go for Kubernetes
- Pointer types for nullable fields
- Direct K8s types vs custom types

### MariaDB Replication
- GTID concepts (gtid_binlog_pos, gtid_slave_pos, gtid_current_pos)
- Replication setup (Server ID, Primary, Replica)
- Switchover flow (7 phases)
- Failover mechanism
- Troubleshooting (Error 1236, split brain)

### Best Practices
- Don't over-engineer - trust well-tested libraries
- Always add tests - even for simple field additions
- Read issue discussions before coding

## Folder Structure

```
concepts/    # Technical concept explanations
issues/      # Issue analysis notes
```

## Related PRs

1. [PR #1588](https://github.com/mariadb-operator/mariadb-operator/pull/1588) - fix: use standard compression extensions ✅ Merged
2. [PR #1589](https://github.com/mariadb-operator/mariadb-operator/pull/1589) - feat: add loadBalancerClass field ✅ Merged
3. [PR #1593](https://github.com/mariadb-operator/mariadb-operator/pull/1593) - feat: add lifecycle field to ContainerTemplate
4. [PR #1594](https://github.com/mariadb-operator/mariadb-operator/pull/1594) - feat: add emptyDir configuration
5. [PR #1595](https://github.com/mariadb-operator/mariadb-operator/pull/1595) - feat: make maxUserConnections nullable
6. [PR #1597](https://github.com/mariadb-operator/mariadb-operator/pull/1597) - feat: use MYSQL_PWD env for security

## License

These notes are for educational purposes. The mariadb-operator project is licensed under Apache 2.0.
