# Issue #940 - Compressed Backup File Extension

**Status**: PR #1588 Created
**Date**: 2026-01-19

## Problem

Logical backups used unconventional file extensions:
- Old: `backup.2023-12-18T16:14:00Z.gzip.sql`
- New: `backup.2023-12-18T16:14:00Z.sql.gz`

Standard tools like `gunzip` couldn't recognize `.gzip.sql` format.

## What I Learned

### 1. Backward Compatibility

When changing file formats, always support both old and new formats for reading:

```go
// New format: backup.timestamp.sql.gz
if parts[2] == "sql" {
    return mariadbv1alpha1.CompressionFromExtension(parts[3])
}

// Legacy format: backup.timestamp.gzip.sql (backward compatible)
calg := mariadbv1alpha1.CompressAlgorithm(parts[2])
```

### 2. Magic Bytes Detection (Concept - Removed in Code Review)

Files have "magic bytes" at the beginning to identify format:

| Format | Magic Bytes | Hex |
|--------|-------------|-----|
| gzip | `\x1f\x8b` | 1F 8B |
| bzip2 | `BZ` | 42 5A |

**Note:** While this is useful knowledge, the implementation was removed per code review feedback (see below).

### 3. Go Testing Patterns

Table-driven tests are common in Go:

```go
tests := []struct {
    name    string
    input   string
    want    string
    wantErr bool
}{
    {"test case 1", "input1", "expected1", false},
    {"test case 2", "input2", "expected2", true},
}

for _, tt := range tests {
    t.Run(tt.name, func(t *testing.T) {
        // test logic
    })
}
```

### 4. Git Workflow

1. Fork the repo
2. Create feature branch (`fix-xxx` or `feat-xxx`)
3. Make changes
4. Run tests and lint
5. Commit with conventional message
6. Push to fork
7. Create PR to upstream

## Files Modified

| File | Changes |
|------|---------|
| `pkg/command/backup.go` | Changed filename format |
| `pkg/backup/processor.go` | Added backward compatibility |
| `pkg/backup/processor_test.go` | Added test cases |
| `pkg/compression/compressor.go` | ~~Added magic bytes validation~~ (removed per code review) |
| `pkg/compression/compressor_test.go` | ~~Added magic bytes tests~~ (removed per code review) |

## Commands Used

```bash
# Run specific tests
go test ./pkg/backup/... -v
go test ./pkg/compression/... -v

# Run lint
make lint

# Create commit
git commit -m "fix: description"

# Create PR
gh pr create --repo upstream/repo
```

## Questions Answered

- **Q: Why use `*string` instead of `string`?**
  A: Pointer allows distinguishing "not set" (nil) from "set to empty string" ("")

- **Q: What is `omitempty` in JSON tags?**
  A: Omits the field from JSON/YAML output when the value is zero/nil

## Code Review Feedback (2026-01-21)

**Maintainer @mmontes11:**
> The backward compatibility change is great! However, I don't think we should introduce additional complexity to enforce the magic bytes. If something is wrong with the compression encoding, the gzip/bzip2 library will return an error.
> Could we omit the magic bytes additional verification?

**Learning:**
- **Don't over-engineer!** If the underlying library already handles errors, don't add extra validation layers
- Keep code simple; complexity should be justified
- Trust well-tested libraries (gzip/bzip2 libraries already validate data internally)

**Action Taken:**
- Removed magic bytes variables (`gzipMagic`, `bzip2Magic`)
- Removed `validateMagicBytes` function
- Removed `expectedMagic` parameter from `decompressFile`
- Removed `TestValidateMagicBytes` test
- Kept backward compatible file extension changes (approved by maintainer)
