# Cloud Radar Source Sync

## Source And Destination

- Source: `aurora@100.124.55.22:/home/aurora/data/rpgfmcw94`
- Target raw directory: `/project/aurora/raw/rpgfmcw94`
- Target Zarr: `/data/aurora/products/rpgfmcw94/cloud_radar.zarr`
- Quicklooks: `/data/aurora/products/quicklooks/cloud_radar`

The source stores hourly radar files under a recursive `Yyyyy/Mmm/Ddd/` tree.
The sync preserves the complete native tree, including LV0/LV1 binaries,
LV0/LV1 NetCDF, and instrument JPG files. Relative paths are unchanged from
edge to cloud raw storage, GWS, and object storage:

```text
/home/aurora/data/rpgfmcw94/Yyyyy/Mmm/Ddd/...
  -> /project/aurora/raw/rpgfmcw94/Yyyyy/Mmm/Ddd/...
  -> /gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/rpgfmcw94/Yyyyy/Mmm/Ddd/...
  -> s3://gamb2le-o/data/incoming/aurora-cloud/raw/rpgfmcw94/Yyyyy/Mmm/Ddd/...
```

Only `*LV1.NC` files feed the dashboard Zarr and its processing-watermark
gate. The other native file classes are archived and verified without being
passed to the LV1 processor.

## Authentication

Radar source sync uses Tailscale SSH over the tailnet IP without private keys.
The script uses regular `ssh` for rsync compatibility, with identity-file and
public-key authentication disabled:

```bash
sudo -u aurora ssh -o IdentityFile=none -o PubkeyAuthentication=no aurora@100.124.55.22 true
```

## Maintained behavior

The source has two independent lanes:

- `aurora-radar-source-sync.service` is the priority live lane. It polls every
  two minutes, uses a bounded bootstrap lookback on first start, overlaps its
  previous checkpoint by ten minutes, and ignores files modified in the last
  five minutes. Its checkpoint is
  `/var/lib/aurora-cloud/radar-live-sync.last`.
- `aurora-radar-backfill.service` is the newest-first historical lane. It
  transfers a bounded batch in parallel and records progress in
  `/var/lib/aurora-cloud/radar-backfill-status.json`. It never advances the
  live checkpoint.

This separation prevents a historical LV0 archive catch-up from delaying new
radar data. The backfill timer is enabled only on the authoritative writer
host; its temporary files live below `.radar-partials` and are excluded from
GWS replication.

To deliberately reset the live sync point, stop the timer and edit or remove
the live checkpoint:

```bash
sudo systemctl stop aurora-radar-source-sync.timer
sudo -u aurora date +%s | sudo tee /var/lib/aurora-cloud/radar-live-sync.last
sudo systemctl start aurora-radar-source-sync.timer
```

## Processing

The radar timers are enabled in Ansible:

- `aurora-radar-append.timer`
- `aurora-radar-quicklooks.timer`

`aurora-radar-source-sync.timer` is enabled once source Tailscale SSH access is
authorized.

## Backup and retention

The archive scope includes the complete native LV0/LV1 binary, NetCDF, and JPG
tree, not only the LV1 NetCDF files used by the dashboard. Object verification
partitions this multi-terabyte family into bounded year/month shards. ASS keeps
each radar file for at least seven days and the retention helper can remove it
only after exact cloud, GWS, and object-store proof. `.radar-partials` is local
interrupted-transfer state and is never archive or deletion evidence. See
[Backups and Archive Services](ARCHIVE_SERVICES.md).

## Capacity and interrupted transfers

Before backfill touches the raw directory, the guard requires the configured
`/project` mount. Before each compressed transfer it checks available bytes on
the destination filesystem. With four workers, the admission requirement is
**8 GiB + four times the largest file selected in this batch**. Every worker
uses the same upper bound, so simultaneous starts are accounted for. This is a
before-transfer reservation, not a quota or protection against unrelated
writers consuming the remaining space. Check the live lane and measured daily
growth separately before resuming either timer.

The helper is `/usr/local/lib/aurora-radar-transfer-guard.py`. A capacity hold
returns 75 before opening a new transfer. The batch does not report success,
and the live checkpoint stays unchanged. Increasing capacity or reducing the
reviewed worker/batch allocation is the recovery path; deleting raw files is not.

Retries use a unique `.incoming.*` partial, preserving previous interrupted
attempts. After a complete, size-validated transfer commits its destination,
the helper checks only partials for that exact file: the legacy name and at
most 16 attempts per worker. A partial must be over six hours old and match the
stable complete target byte-for-byte for its entire length. The target is
fsynced, and a SHA-256 audit record is fsynced before the partial is removed.
Mismatches, recent attempts, symlinks and changed files stay in place. This
bounded cleanup never removes a completed observation. Remaining old partials
need a separately reviewed recovery pass; this helper does not sweep the tree.

Deploy the helper and backfill wrapper together while backfill is stopped.
The focused `playbooks/radar_backfill_guard.yml` requires the current installed
wrapper's SHA-256 in `aurora_radar_backfill_expected_sha256`, checks the idle
worker and mounted reserve, and backs up both replaced files. Review it with
`--check --diff` before applying. It does not start or enable either radar timer.
Do not apply the broad source-sync role just to install this guard.
Validate the existing mount and the guard read-only before restoring its timer:

```bash
sudo -u aurora /usr/local/lib/aurora-radar-transfer-guard.py capacity \
  --root /project/aurora/raw/rpgfmcw94 --required-mount /project \
  --workers 4 --max-file-bytes <largest-selected-file-bytes> --reserve-bytes 8589934592
```

Record the checked reservation, free capacity, deployed source revision and the
next scheduled result. Installing this guard is not evidence that the backfill
has resumed or that the long-term capacity requirement is met.
