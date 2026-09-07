# HATPRO Source Sync

## Source And Destination

- Source: `aurora@100.124.55.22:/home/aurora/data/hatprog5`
- Target raw directory: `/project/aurora/raw/hatprog5`
- Target Zarr: `/data/aurora/products/hatprog5/hatpro.zarr`

The source stores HATPRO files under a recursive `Yyyyy/Mmm/Ddd/` tree. The
sync mirrors all files whose names begin with `HATPROG5-AURORA-ICELAND_`,
including both raw companion files and NetCDF products. The Zarr builder uses
the NetCDF products for:

- `*.LWP.NC`
- `*.IWV.NC`
- `*.IRT.NC`
- non-CMP `*.TPC.NC`
- `*.TPB.NC`
- `*.CMP.TPC.NC`
- `*.MET.NC`

## Authentication

HATPRO source sync uses Tailscale SSH over the tailnet IP without private keys.
The script uses regular `ssh` for rsync compatibility, with identity-file and
public-key authentication disabled:

```bash
sudo -u aurora ssh -o IdentityFile=none -o PubkeyAuthentication=no aurora@100.124.55.22 true
```

## Current Deployed Behavior

`aurora-hatpro-source-sync.service` runs `/usr/local/bin/aurora-hatpro-sync`.
Its path-aware discovery driver inventories matching source relative paths,
sizes and modification times. It selects new or changed paths even when their
mtime is older than `/var/lib/aurora-cloud/hatpro-sync.last`. A flat file moved
into `Yyyyy/Mmm/Ddd/` or an entire renamed directory cannot be hidden by the
old timestamp cursor. Destination size/mtime differences also qualify for the
existing rsync copier; unchanged files are not transferred.

The existing two-minute live timer, six-hour backfill timer, authentication,
`rsync -a --partial` copy implementation and shared archive dispatcher are
unchanged. Live discovery shares the existing backfill lock and defers on
contention rather than running overlapping HATPRO destination copies.

Discovery persists exact pending handoffs before copying. A failed listing,
copy or archive enqueue does not advance the cursor past unsubmitted work.
Pending handoffs survive process restarts and are replayed even if the cloud
file already exists. If a source path moves while pending, fresh discovery
finds its replacement path; an old path is enqueued only when its completed
cloud copy still matches the saved metadata. Source files and old cloud copies
are never deleted. A successful enqueue is delivery work, not verified archive
parity; independent GWS and S3 checks still determine health and retention.

Malformed/truncated listings, unsafe destination symlinks, invalid source
metadata and corrupt or differently bound checkpoints fail explicitly. They
are never interpreted as an empty inventory or a reset cursor. State files
`hatpro-sync.last.pending.json` and `hatpro-sync.last.baseline.json` reside
alongside the cursor, with restricted permissions and atomic/fsynced writes.
Do not hand-edit those files to suppress a failure.

When the cursor is absent in production `start_fresh: false` mode,
discovery reconciles the current source history before advancing the cursor.

If you deliberately want a fresh-start behavior, set
`hatpro_source_start_fresh: true` before the first run. That mode records an
explicit path/metadata baseline and the current epoch without copying old
history. Later new relative paths or changed metadata are still discovered,
including files with old mtimes. Existing fresh-start cursors migrate their
old-history baseline conservatively. Changing configured source bindings
with existing discovery state is an explicit block, not permission to reuse
checkpoints for a different source.

Use `playbooks/hatpro_discovery.yml` with a unique `hatpro_discovery_release`
to deploy only the discovery driver and wrapper. The playbook reads live
source bindings, defers while sync/backfill is busy, verifies restricted
rollback copies, and atomically installs the two code files. It does not
restart services, alter either schedule, reset the cursor, change acquisition
or touch archive verification/retention state. The next existing timer run
performs reconciliation. Validate actual copied paths, durable dispatch,
fresh independent archive evidence and the public API before claiming an
incident resolved. Roll back only the recorded code files; do not restore a
historical cursor over runtime progress or change source files.

## Gap reconciliation

`aurora-hatpro-backfill.timer` runs an independent idempotent tree
reconciliation every six hours. It copies only source paths absent from the
cloud raw archive and queues only the files actually copied for GWS and object
storage delivery. This is deliberately separate from the live timestamp cursor:
files recovered on ASS with an older mtime must still reach both archives.
The live path-aware scan now also discovers these gaps on its normal schedule;
the existing backfill remains a separate safety net.

For an incident repair, run one bounded manual reconciliation and observe it
before relying on the timer:

```bash
sudo systemctl start aurora-hatpro-backfill.service
sudo journalctl -u aurora-hatpro-backfill.service -n 100 --no-pager
```

## Processing

The deployed HATPRO processing timer is:

- `aurora-hatpro-append.timer`
- `aurora-hatpro-quicklooks.timer`

The HATPRO builder appends new samples by default. Each timer run scans a
bounded lookback window before the current Zarr frontier, prefers canonical
`Yyyyy/Mmm/Ddd/` files over old top-level mirror duplicates, and appends only
samples newer than the product. Use `hatpro_to_zarr.py --rebuild` only for an
intentional full product rewrite.

Mirror verification also ignores legacy top-level HATPRO duplicates and compares
only the canonical recursive raw tree against source and GWS manifests. If a
source batch was mirrored flat before this policy, run one preservation rsync
from the source root to backfill the missing `Yyyyy/Mmm/Ddd/` paths before
allowing pruning.

The Zarr keeps the standard temperature profile (`T_PROF`) separate from the
composite temperature profile (`T_PROF_CMP`) because those source files can
share timestamps while containing different profile values.

## Backup and retention

The canonical recursive `Yyyyy/Mmm/Ddd/` raw tree is archived additively to
both GWS and object storage. Legacy flat duplicates are ignored by parity and
cannot authorize pruning. ASS keeps a canonical raw file for at least seven
days and deletes it only through an exact signed permit after cloud, GWS, and
object-store proof. The HATPRO Zarr and quicklooks are product archives, not
raw-retention evidence. See [Backups and Archive Services](ARCHIVE_SERVICES.md).
