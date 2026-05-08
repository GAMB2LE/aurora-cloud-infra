# Wxcam Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/wxcam`
- Target raw directory: `/project/aurora/raw/wxcam`
- Catalog: `/data/aurora/products/wxcam/wxcam_catalog.sqlite`
- Daily videos: `/data/aurora/products/wxcam/daily_videos`
- Pixel Zarr path: `/data/aurora/products/wxcam/wxcam.zarr` (service installed, timer currently disabled)

The wxcam source contains nested `FISH/` and `PANO/` trees. The deployed sync
copies the full raw tree instead of filtering by extension so the local archive
keeps JPG frames and MP4 clips together.

## Dashboard behavior

- Dashboard instrument name: `wxcam`
- Interactive tab: primary wxcam browser and player
- Calendar tab: intentionally blank for wxcam

The dashboard uses the SQLite catalog plus daily MP4 products for browsing. It
does not currently rely on the wxcam pixel Zarr path.

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

`aurora-wxcam-append.timer` is installed but intentionally disabled while the
pixel-Zarr design is still under review.

The sync script uses `/var/lib/aurora-cloud/wxcam-sync.lock` so a long-running
full-tree rsync cannot overlap with the next timer tick.
