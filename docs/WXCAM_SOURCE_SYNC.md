# Wxcam Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/wxcam`
- Target raw directory: `/project/aurora/raw/wxcam`
- Catalog: `/data/aurora/products/wxcam/wxcam_catalog.sqlite`
- Daily videos: `/data/aurora/products/wxcam/daily_videos`
- Hourly thumbnails: `/data/aurora/products/wxcam/hourly_thumbnails`
- Pixel Zarr path: `/mnt/gws/gamb2le/data/output/aurora-cloud/products/wxcam/wxcam.zarr`

The wxcam source contains nested `FISH/` and `PANO/` trees. The deployed sync
retains HDR JPG and MP4 files from both streams locally. `AUTO`/`LONG`/`SHORT`
files remain on the camera host and are not cataloged, Zarr-appended, or
archived from this VM.

## Dashboard behavior

- Dashboard instrument name: `WXcam`
- Interactive Data Browser: primary wxcam browser and player using stitched MP4 products
- Science Quicklooks: hourly JPG thumbnail grid

The dashboard uses the SQLite catalog plus daily MP4 and hourly thumbnail
products for browsing. The science-quicklook grid is driven by the selected
HDR JPG stream, while the interactive browser uses stitched HDR MP4 products.
The wxcam pixel Zarr is GWS-only and starts at `2026-07-04T00:00:00Z`.
Local raw/catalog/video products remain on the processing host, but the
decoded pixel Zarr is written through the GWS SSHFS mount.

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
rsync cannot overlap with the next timer tick.
