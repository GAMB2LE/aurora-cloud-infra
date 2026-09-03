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

The operational sync scans only the current and previous UTC date folders. It
transfers files newer than `/var/lib/aurora-cloud/wxcam-sync.last`, with a
ten-minute overlap for late writes, and advances that checkpoint only after a
successful rsync. A missing, invalid, or older-than-window checkpoint resumes
at the live edge. Historical backfill is a separate manual operation and cannot
block the two-minute live-data timer.

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
