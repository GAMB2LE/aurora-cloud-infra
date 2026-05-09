# Wxcam Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/wxcam`
- Target raw directory: `/project/aurora/raw/wxcam`
- Catalog: `/data/aurora/products/wxcam/wxcam_catalog.sqlite`
- Daily videos: `/data/aurora/products/wxcam/daily_videos`
- Hourly thumbnails: `/data/aurora/products/wxcam/hourly_thumbnails`
- Pixel Zarr path: `/data/aurora/products/wxcam/wxcam.zarr`

The wxcam source contains nested `FISH/` and `PANO/` trees. The deployed sync
keeps only HDR JPGs and HDR hourly MP4 clips in the local mirror, while
leaving the broader remote source archive untouched.

## Dashboard behavior

- Dashboard instrument name: `WXcam`
- Interactive tab: primary wxcam browser and player using stitched MP4 products
- Calendar tab: hourly JPG thumbnail grid

The dashboard uses the SQLite catalog plus daily MP4 and hourly thumbnail
products for browsing. The calendar grid is driven by HDR JPG selections,
while the interactive browser uses stitched MP4 products. The wxcam pixel Zarr
path is built from the HDR JPG archive.

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
