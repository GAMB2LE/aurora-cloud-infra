# Cloud Radar Source Sync

## Source And Destination

- Source: `aurora@100.124.55.22:/home/aurora/data/rpgfmcw94`
- Target raw directory: `/project/aurora/raw/rpgfmcw94`
- Target Zarr: `/data/aurora/products/rpgfmcw94/cloud_radar.zarr`
- Quicklooks: `/data/aurora/products/quicklooks/cloud_radar`

The source stores hourly radar files under a recursive `Yyyyy/Mmm/Ddd/` tree.
The sync preserves the complete native tree, including LV0/LV1 binaries,
LV0/LV1 NetCDF, and instrument JPG files. Relative paths are unchanged from
edge to cloud raw storage and GWS:

```text
/home/aurora/data/rpgfmcw94/Yyyyy/Mmm/Ddd/...
  -> /project/aurora/raw/rpgfmcw94/Yyyyy/Mmm/Ddd/...
  -> /gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/rpgfmcw94/Yyyyy/Mmm/Ddd/...
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

## Current Deployed Behavior

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
