# Archives and alerts

I check delivery, verification and permission to delete separately. A successful
copy, a fresh dashboard and a clean archive report answer different questions.

For `products` and `products-wxcam`, a successful exact-path repair also checks
the reported objects individually for presence and size. Confirmation is
recorded in the existing repair-result JSON and bound to the family audit ID
and unchanged local files. If every reported gap is confirmed and no other
failure requires red, the dashboard shows amber: **Repair verified; full audit
continuing**. A newer audit, changed source or expired evidence invalidates
that confirmation. This display status does not certify parity or change
pruning permission. Checksum findings still require the full audit.

To check an already-repaired product without another copy or queue change, use
`aurora-object-store-repair-from-report --verify-only --job products`. This
publishes the same bounded confirmation; it does not run a full audit.

## Where the copies go

| Copy | Location | Purpose |
| --- | --- | --- |
| Instrument source | ASS or APS | Original observations |
| Production raw | `/project/aurora/raw` | Working raw copy on JASMIN |
| Production products | `/data/aurora/products` | Zarrs, quicklooks and other derived files |
| GWS raw | `/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw` | Additive archive |
| Object-store raw | `s3://gamb2le-o/data/incoming/aurora-cloud/raw` | Independent additive archive |
| Development mirror | Data-ocean | Display continuity and development, not retention proof |

Source sync lands files on production and queues their exact paths. The
newest-first dispatcher sends bounded batches to GWS and object storage. It
tracks the two destinations separately. Full-tree writers work through older
files alongside it. Neither archive propagates source deletions: GWS uses
rsync without delete options; object writers use `rclone copy`.

`aurora_archive_streams` in `inventory/group_vars/aurora_cloud.yml` defines the
managed sources, paths and retention policy. A file outside that list cannot
be pruned just because a bulk writer copied it.

## What the alert means

The producer writes `/data/aurora/internal/archive_status/health-v1.json`.
Its schema is `health-v2`; the filename stays unchanged for older clients.
The dashboard and notification code read this contract rather than inventing
another archive check.

| Result | What I do |
| --- | --- |
| Recent file is pending | Check progress; normal settling and copying are not an alert |
| Missing path is overdue, or a settled file differs | Investigate the named path and destination, then verify the repair |
| A transient listing retry has current clean evidence | Let automatic recovery resume its checkpoint |
| Evidence expired, a worker failed permanently or progress stopped | Investigate verification; this does not by itself prove data loss |
| One clean report | Wait for the required independent confirmation |
| `prune_ready=true` on one stream | Check the complete signed-retention gate; this flag alone permits no deletion |

### The hourly radar window

Native `P01_ZEN` `.LV0`, `.LV1` and `.LV0.NC` files need 45 minutes without a
source write, followed by up to 30 minutes for delivery. A missing path inside
that **75-minute window** is pending. After that it alerts. The clock starts
at the source's last write, not the filename's acquisition start.

The verifier keeps the raw missing counts and adds exact-path
`path_alert_grace` evidence. Health publishes separate `pending_*` and
`alert_*` counts and rechecks expiry against the current time. An old snapshot
cannot extend the allowance. Invalid grace evidence does not suppress a gap.
Mismatches, other streams and independent verification failures still alert.
This changes alert presentation, not archive proof or retention eligibility.

## Verification windows

Copying starts before a file is old enough for the independent archive audit.
These are separate clocks:

| Job | Writer minimum age | Object verification age |
| --- | ---: | ---: |
| Raw | 15 minutes | 6 hours |
| Products | 20 minutes | 30 hours |
| WXcam products | 30 minutes | 30 hours |
| Model evaluation | 1 hour | 6 hours |
| Manifests | 5 minutes | 2 hours |

Younger product files appear as `pending_upload`. They become gaps if still
missing or mismatched after their verification window. Source mtimes must be
finite and positive; an invalid timestamp is a source fault, not a settled file.

Raw evidence expires after eight hours. Product evidence has its own 36-hour
limit. A product refresh cannot renew an older raw proof. Each gate needs two
distinct clean confirmations and complete coverage of its required families.
Reused evidence retains its original timestamp.

The source/cloud/GWS verifier also reports per-stream metadata comparisons.
HATPRO flat paths stay in scope while the source still requires those exact
paths. A same-basename file elsewhere is not a replacement.

## Normal operation

- Source sync submits exact newly landed paths to the dispatcher, normally every
  two minutes. Its default batch is at most 5,000 files or 20 GiB.
- Raw GWS bulk copies and dispatch share a delivery lock. Dispatch gets priority
  between bounded bulk batches. A busy bulk attempt defers; it does not evade
  the lock by switching transfer hosts.
- Writers give recent files priority, then spend a bounded slice on history.
  Partial copies and temporary files are not archive or deletion evidence.
- The recovery coordinator checks its durable queue every five minutes. Raw
  refresh is due after three hours; other families after twelve. The complete
  daily audit is requested at 03:20 UTC. Actual starts may be later.
- Archive health is published every two minutes. Check installed timers and
  the live catalogue for writer schedules; repository defaults are not a host
  status report.

The automatic collector is described in [Archive recovery](ARCHIVE_RECOVERY.md).
Do not start a second inventory against its production catalogue.

## Check a problem

On production, start with these read-only commands:

```bash
sudo aurora-object-store-recovery status
systemctl status aurora-archive-dispatch.service --no-pager
systemctl status aurora-mirror-verify.service --no-pager
cat /data/aurora/internal/archive_dispatch/status.json
cat /data/aurora/internal/archive_status/health-v1.json
```

Follow the named path from source to production raw, GWS and object storage.
Compare the exact version, not just its name. A dispatcher receipt records a
completed transfer command; independent metadata and byte readbacks establish
archive delivery. A clean scan describes its own observation, not later files.

Exact repair copies only reported paths. It invalidates the affected evidence
before writing and requests fresh confirmations afterward. Keep an unresolved
operation outstanding until its worker and destination have been reconciled.
A timeout or a missing PID is not success. Do not clear partials, queue rows,
checkpoints or warnings to make the dashboard green.

## Retention and deployment

ASS keeps managed source files for at least seven days. Only a short-lived,
exact signed permit backed by production, GWS and object-store proof can
remove them. APS Power remains non-prunable. An operator hold remains binding
even when dynamic archive checks are green. See
[ASS retention](ASS_BACKUP_AND_RETENTION.md) for the permit and audit checks.

Use focused playbooks and review `--check --diff` before applying. A normal
verifier or health template install now refuses to replace a storage-guarded
entrypoint. Those hosts need a pinned-payload release that preserves the
storage wrapper and its lifetime lock. Check the executing payload and the
next published result; hashing the public wrapper alone is not enough.

The dashboard consumer playbook does not install writers or pruning services.
Use [Releases](RELEASES.md) for release boundaries and rollback, and
[Archive testing](ARCHIVE_TESTING.md) for the required checks.
