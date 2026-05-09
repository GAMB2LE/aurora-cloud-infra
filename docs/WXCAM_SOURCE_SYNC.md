# Wxcam Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/wxcam`
- Target raw directory: `/project/aurora/raw/wxcam`
- Catalog: `/data/aurora/products/wxcam/wxcam_catalog.sqlite`
- Daily videos: `/data/aurora/products/wxcam/daily_videos`
- Hourly thumbnails: `/data/aurora/products/wxcam/hourly_thumbnails`
- Pixel Zarr path: `/data/aurora/products/wxcam/wxcam.zarr`

The wxcam source contains nested `FISH/` and `PANO/` trees. The deployed sync
mirrors the full raw tree into `/project/aurora/raw/wxcam` so the local raw
copy can become authoritative for retention and downstream archival checks.
Dashboard products still use only the HDR JPG and HDR MP4 subsets.

During a large historical backfill, the sync script first refreshes the newest
day directories for both streams and then falls back to the full-tree rsync.
That keeps current-day WXcam products closer to real time while the archive
catch-up continues in the background.

## Dashboard behavior

- Dashboard instrument name: `WXcam`
- Interactive Data Browser: primary wxcam browser and player using stitched MP4 products
- Science Quicklooks: hourly JPG thumbnail grid

The dashboard uses the SQLite catalog plus daily MP4 and hourly thumbnail
products for browsing. The science-quicklook grid is driven by HDR JPG
selections, while the interactive browser uses stitched MP4 products. The
wxcam pixel Zarr path is built from the HDR JPG archive, even though the raw
mirror includes the full upstream tree.

The catalog, daily-video, and pixel-Zarr timers are intentionally allowed to
run while a long raw backfill is still in progress. Fresh in-flight media are
deferred until they have settled, so current products can keep refreshing
during large archive syncs instead of waiting for the full mirror to finish.

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
- `aurora-wxcam-catalog.timer`
- `aurora-wxcam-daily-videos.timer`
- `aurora-wxcam-append.timer`

The sync script uses `/var/lib/aurora-cloud/wxcam-sync.lock` so a long-running
full-tree rsync cannot overlap with the next timer tick.
