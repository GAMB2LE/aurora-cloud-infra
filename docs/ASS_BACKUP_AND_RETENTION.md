# ASS Backup and Seven-Day Retention

This page describes deletion at the ASS source. The complete writer,
verification, repair, and status model is in
[Backups and Archive Services](ARCHIVE_SERVICES.md).

## Contract

Files on ASS are retained for **at least** seven days. A file may remain longer
when an archive, verifier, or permit is unavailable. A file may be removed from
ASS only when the same immutable source-relative path is proven present and
matching in:

1. the production cloud raw mirror;
2. the JASMIN GWS raw archive; and
3. the object-store raw archive.

No service is permitted to use a broad recursive delete. Pruning is performed
only for manifest-listed regular files, in bounded batches. Empty directories
are not part of the permit and are not recursively removed.

## Flow

```text
ASS source
    |
    v
production cloud raw mirror
    |                    |
    v                    v
GWS raw archive     object-store raw archive
    |                    |
    +-- immutable exact verification evidence --+
                                                  |
                                      short-lived signed permit
                                                  |
                             edge recheck and exact-file unlink
```

## Gates

- The GWS verifier must have a fresh immutable manifest. For every enabled ASS
  stream, its seven-day age-bounded source-to-cloud and source-to-GWS missing
  and mismatch counts must all be zero.
- The object-store inventory must be fresh and report no settled raw missing
  paths, size mismatches, or checksum mismatches. Raw verification stays
  strict at a six-hour horizon.
- The object-store stability gate must describe that exact inventory, record
  the independently additive writer policy, and have passed the configured
  number of consecutive clean inventories (two by default). Fresh derived
  products inside their 30-hour settle window are `pending_upload` and do not
  reset this streak; an older product gap still does.
- The source path must be present in the GWS verifier's exact
  `prune_candidates.tsv` and have an mtime older than seven days.
- The cloud coordinator creates a short-lived, site-specific permit containing
  only exact relative paths, sizes, mtimes, and the seven-day cutoff.
- The edge host revalidates that permit with the root-owned
  `/usr/local/sbin/aurora-edge-prune-exact` helper. The helper rejects expired,
  oversized, traversing, symlinked, changed, or out-of-root candidates.
- Any stale report, failed service, incomplete stream, or missing object blocks
  the whole run without deleting a file.

`prune_ready=true` on one stream is not permission by itself. It means the GWS
verifier has exact candidates for that stream. The global object-store gate,
fresh immutable report identities, signed permit, and edge rechecks must still
pass in the same run.

## Services

- `aurora-object-store-copy-raw.service` performs additive full-history raw
  archival; it never deletes object-store data.
- `aurora-mirror-verify.service` proves source-to-cloud-to-GWS equivalence and
  generates exact prune candidates.
- `aurora-object-store-inventory.service` compares settled cloud data with GWS
  and object storage and reports younger product files separately as pending.
- `aurora-ass-retention.timer` runs daily at 03:30 UTC.  Its service is
  fail-closed and asks the restricted edge helper to remove only the exact
  permitted ASS paths older than seven days.

The reusable roles default to dry-run and disabled scheduling. The committed
production host policy explicitly enables the cloud timer and live ASS helper;
that override is visible in `inventory/host_vars/aurora-cloud.yml` and the ASS
edge host vars. All prune-managed ASS raw streams, including the complete radar
LV0/LV1 tree, use a seven-day source-retention window. APS power remains
non-prunable regardless of the global cloud mode. Committed values describe
the intended state; use current unit status and audit receipts to prove what a
host actually did.

The signing private key is generated out of band and is never committed or
copied to the edge. On the cloud coordinator it is owned by `root`, readable
only by the coordinator service group (`0640`), and its parent directory is
`0750`. Only the derived public key is installed on ASS.

Initial production activation is two-stage. Deploy both sides in live mode
while leaving `aurora-ass-retention.timer` disabled, then run a manual bounded
canary with `aurora-ass-retention --canary --max-candidates N` (at most 5,000).
The manual canary requires one fresh globally clean inventory. Only after its
cloud and edge receipts, exact deletions, continued ingest, archive health, and
disk recovery are checked should the daily timer be enabled.

Normal scheduled retention is stricter: it requires stable parity from two
distinct clean inventories. The production coordinator considers no more than
50,000 candidates per daily run, round-robins the oldest eligible files across
streams, and sends permits in batches of at most 500 paths. The edge helper
also caps each permit at 500 candidates. These are workload bounds, not weaker
verification rules.

Raw-data proof is independent of the newest derived-product files. A file is
eligible only when its source-to-cloud, source-to-GWS, and cloud-to-object raw
checks pass, it is older than the stream's retention window, and the ASS helper
accepts its short-lived signed permit. A new Zarr chunk or quicklook inside the
30-hour product settle window therefore cannot block retention. A settled
product gap older than that window remains a global archive-health failure and
must be repaired.

Files outside the managed archive scope remain on ASS until an explicit source,
GWS, and object-store mapping is added; the retention service must not guess.

Every decision and candidate is written to append-only JSONL audit logs on the
cloud coordinator and edge host.

## Evidence and operator checks

The authoritative cloud evidence is:

| Evidence | Path |
| --- | --- |
| Latest GWS verification | `/data/aurora/internal/mirror_manifests/latest/summary.json` |
| Immutable GWS histories | `/data/aurora/internal/mirror_manifests/history/` |
| Latest object comparison | `/data/aurora/internal/object_store_manifests/latest/comparison.json` |
| Object inventory progress | `/data/aurora/internal/object_store_manifests/progress.json` |
| Stable-parity gate | `/var/lib/aurora-cloud/object-store-verification-gate/state.json` |
| Published archive health | `/data/aurora/internal/archive_status/health-v1.json` |
| Cloud retention receipts | `/data/aurora/internal/retention/` |

The edge audit log is `/var/log/aurora-edge/retention.jsonl`. A successful
service exit alone is not proof of deletion: match the permit/report IDs and
exact paths across both audit locations, then confirm free space and continued
source ingest.
