# ASS Backup and Seven-Day Retention

## Contract

Files on ASS are retained for seven days.  A file may be removed from ASS only
when the same immutable source-relative path is proven present and matching in:

1. the production cloud raw mirror;
2. the JASMIN GWS raw archive; and
3. the object-store raw archive.

No service is permitted to use a broad recursive delete.  Pruning is performed
only for manifest-listed paths, in bounded batches.

## Flow

```text
ASS source -> cloud raw mirror -> GWS raw + object-store raw
                                     |             |
                                     +-- manifests -+
                                             |
                              fresh, exact verification gate
                                             |
                         ASS paths older than seven days only
```

## Gates

- The GWS verifier must have a fresh manifest and mark every applicable stream
  `prune_ready`.
- The object-store inventory must be fresh and report no raw missing paths,
  size mismatches, or checksum mismatches.
- The source path must be present in the GWS verifier's exact
  `prune_candidates.tsv` and have an mtime older than seven days.
- The cloud coordinator creates a short-lived, site-specific permit containing
  only exact relative paths, sizes, mtimes, and the seven-day cutoff.
- The edge host revalidates that permit with the root-owned
  `/usr/local/sbin/aurora-edge-prune-exact` helper. The helper rejects expired,
  oversized, traversing, symlinked, changed, or out-of-root candidates.
- Any stale report, failed service, incomplete stream, or missing object blocks
  the whole run without deleting a file.

## Services

- `aurora-object-store-copy-raw.service` performs additive full-history raw
  archival; it never deletes object-store data.
- `aurora-mirror-verify.service` proves source-to-cloud-to-GWS equivalence and
  generates exact prune candidates.
- `aurora-object-store-inventory.service` compares cloud raw data with GWS and
  the object store.
- `aurora-ass-retention.timer` runs daily at 03:30 UTC.  Its service is
  fail-closed and asks the restricted edge helper to remove only the exact
  permitted ASS paths older than seven days.

The coordinator and edge helper default to dry-run. Enabling deletion requires
both sides to be explicitly configured for live mode. Radar and APS power remain
non-prunable regardless of the global mode.

Files outside the managed archive scope remain on ASS until an explicit source,
GWS, and object-store mapping is added; the retention service must not guess.

Every decision and candidate is written to append-only JSONL audit logs on the
cloud coordinator and edge host.
