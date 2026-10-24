# WXcam Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/wxcam`
- Target raw directory: `/project/aurora/raw/wxcam`
- Catalog: `/data/aurora/products/wxcam/wxcam_catalog.sqlite`
- Daily videos: `/data/aurora/products/wxcam/daily_videos`
- Hourly thumbnails: `/data/aurora/products/wxcam/hourly_thumbnails`
- Local derived pixel Zarr:
  `/data/aurora/products/wxcam/wxcam.zarr`

The WXcam source contains nested `FISH/` and `PANO/` trees. The deployed sync
retains HDR JPG and MP4 files from both streams locally. `AUTO`/`LONG`/`SHORT`
files remain on the camera host and are not cataloged, Zarr-appended, or
archived from this VM.

The operational sync scans the current and previous UTC date folders, with a
ten-minute overlap from `/var/lib/aurora-cloud/wxcam-sync.last`. A changed media
scope also selects the complete settled HDR history. Each run transfers three
phases: the newest three settled JPEGs from each camera, the remaining JPEGs,
and then MP4 files. Separate rsync calls preserve that order.

Each phase has a 120-second transfer budget, set by
`wxcam_source_transfer_timeout_seconds`, with up to 15 seconds to terminate a
stuck transfer. A timeout returns a nonzero service result; that is unfinished
work, not a successful sync. Completed phases are queued for raw archiving
immediately. The global checkpoint advances only after all three phases and
their queue submissions succeed. Rsync partials are retained for retry.

Selected paths are saved atomically in `wxcam-sync.last.pending` before any
transfer. The next run merges that debt with newly discovered files, so an old
video cannot disappear when it ages out of the two-day scan. The pending file
is removed only after the whole run succeeds. Do not clear it or advance the
checkpoint to hide a timeout. Missing, invalid or stale checkpoints may resume
at the live edge only when no pending work exists. Historical gaps outside the
selected set still need reconciliation or an explicit bounded recovery.

To update only the existing live wrapper, inspect the focused dry run and then
apply it. The playbook checks shell syntax, keeps a rollback copy, and leaves
timers, archive configuration, checkpoints and all scientific writers alone.
An already running sync finishes with its loaded script; the next timer tick
uses the new wrapper. Do not stop a working bulk copy merely to deploy this.

```bash
uv run ansible-playbook playbooks/wxcam_live_sync.yml --limit aurora-cloud --check --diff
uv run ansible-playbook playbooks/wxcam_live_sync.yml --limit aurora-cloud
```

To roll back, restore the template task's recorded backup to
`/usr/local/bin/aurora-wxcam-sync`. Keep the pending file and cursor. An older
wrapper does not consume that pending file, so restore the pending-aware
wrapper before accepting a later checkpoint as evidence that debt is complete.

An independent six-hour reconciliation pass scans the ten most recently
completed UTC date folders without consulting the live checkpoint or applying
a lower mtime bound. This catches late arrivals and recovered files that retain
an old timestamp. It also requires both mtime and ctime to be settled, skips the
current UTC folder, and uses `rsync --ignore-existing`: existing cloud raw files
are never overwritten and no source or destination file is deleted. Only paths
reported by rsync as newly copied files are submitted to the raw archive queue.
Rsync logs each receipt after transfer completion, and the service persists
those exact records in `/var/lib/aurora-cloud/wxcam-reconcile.pending`. Pending
records are deduplicated and replayed before new work; the journal is removed
only after the durable archive queue accepts it.
The ten-day window deliberately exceeds the seven-day edge-retention hold, so
ordinary on-time files are checked repeatedly before they can become pruning
candidates. It is not an all-history recovery scan: when files are restored
into capture-date folders older than ten completed days, temporarily extend
`wxcam_reconcile_lookback_days` for that bounded recovery and return it to the
commissioned value after archive parity is verified.

The focused `wxcam_reconcile` Ansible tag installs only this script and its
units, enables or disables its timer according to policy, and refreshes the
archive-health collector so it monitors the new auxiliary unit immediately.
It does not reconfigure other archive or source-sync units. Run it first with
`--check --diff` and an explicit production `--limit`; because the collector is
a shared generated executable, verify that its diff contains no unrelated
revision drift before applying.

```bash
uv run ansible-playbook playbooks/archive_services.yml \
  --limit aurora-cloud --tags wxcam_reconcile --check --diff
uv run ansible-playbook playbooks/archive_services.yml \
  --limit aurora-cloud --tags wxcam_reconcile
```

Running `archive_services.yml` without the tag reapplies the complete archive,
verification, monitoring, and retention stack and is not a focused WXcam
release.

## Dashboard behavior

- Dashboard instrument name: `WXcam`
- Interactive Data Browser: primary wxcam browser and player using stitched MP4 products
- Science Quicklooks: hourly JPG thumbnail grid

The dashboard uses the SQLite catalog plus daily MP4 and hourly thumbnail
products for browsing. The science-quicklook grid is driven by the selected
HDR JPG stream, while the interactive browser uses stitched HDR MP4 products.
The wxcam pixel Zarr is a local mutable working store and starts at
`2026-07-04T00:00:00Z`. It is reproducible from the archived HDR imagery and
is deliberately excluded from the additive GWS and object-store product
writers. Immutable raw HDR files, the catalog, daily videos, and hourly
thumbnails are archived. The derived Zarr is never accepted as retention
evidence.

The catalog, daily-video, and pixel-Zarr timers consume the incrementally
updated raw mirror. Fresh in-flight media are deferred until they have settled.
Check source, raw, catalogue and rendered-media times separately: a fresh raw
JPEG does not prove its thumbnail or daily video has been rebuilt yet.

## Authentication

The sync uses Tailscale SSH over the tailnet. The rsync remote shell is regular
`ssh` with identity keys disabled:

- `IdentitiesOnly=yes`
- `IdentityFile=none`
- `PubkeyAuthentication=no`
- `StrictHostKeyChecking=accept-new`

No private key is installed for this source.

## Timers

- `aurora-wxcam-source-sync.timer`
- `aurora-wxcam-reconcile.timer`
- `aurora-wxcam-catalog.timer`
- `aurora-wxcam-daily-videos.timer`
- `aurora-wxcam-append.timer`

Reconciliation uses its own run lock to serialize its persistent receipt
journal. The live sync and reconciliation scripts also share
`/var/lib/aurora-cloud/wxcam-sync.lock`, so they cannot race to create the same
missing destination path. Reconciliation performs source discovery first,
holds the shared lock only for its copy-only rsync, and then releases it before
archive queue submission. A live tick that encounters the lock exits safely;
the next available two-minute tick resumes normal ingestion. Reconciliation
also suppresses directory timestamp, permission, owner, and group changes, so
its only archive-tree mutations are new in-scope files and any parent
directories they require.

## Backup and retention

Archive and retention scope is deliberately limited to FISH/PANO HDR JPG and
MP4 files. AUTO/LONG/SHORT files remain outside this cloud-managed backup and
cannot be pruned by it. In-scope HDR media is archived additively to the raw
prefixes on GWS and object storage and retained on ASS for at least seven days.
The SQLite catalog, daily videos, and hourly thumbnails are product archives;
the local mutable `wxcam.zarr` is excluded because it is reproducible from raw
HDR imagery. A fresh product that is not yet archived is reported as
`pending_upload` during its 30-hour settle window; it participates in stable
parity only after that window. See
[Backups and Archive Services](ARCHIVE_SERVICES.md).
