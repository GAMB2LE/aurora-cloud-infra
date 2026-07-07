# Aurora Data Locations

Last checked: 2026-07-07 UTC.

This page summarizes where Aurora data lives across the source hosts, the
active dashboard processor, and the GWS archive. The Ansible inventory remains
the source of truth for configured paths; the live start times below were read
from the active droplet on 2026-07-07.

## Storage Roots

| Layer | Path or host | Purpose |
| --- | --- | --- |
| ASS Linux source | `aurora@100.124.55.22:/home/aurora/data` | Main Aurora source host for CL61, radar, HATPRO, Vaisala MET, ASFS, WXcam, and MX4/AURORACam ingest. |
| APS Linux source | `aurora@100.81.226.30:/data/power/level1` | Aurora Power Supply level-1 source files. |
| Local raw mirror | `/project/aurora/raw` | Droplet-side mirrored source files. Product rebuilds should start here, not from GWS. |
| Local products | `/data/aurora/products` | Dashboard-serving products: Zarrs, quicklooks, catalogs, daily videos, health reports, and small derived stores. |
| Local state | `/var/lib/aurora-cloud` | Sync markers, append state, lock files, and operational state. |
| Local logs | `/var/log/aurora-cloud` | Service logs for source syncs, appenders, quicklooks, and GWS jobs. |
| Dashboard app | `/opt/aurora-cloud-dashboard` | Deployed dashboard code and virtualenv. |
| GWS raw archive | `/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw` | Archived copy of `/project/aurora/raw`. |
| GWS product archive | `/gws/ssde/j25b/gamb2le/data/output/aurora-cloud/products` | Archived product copy and the intended WXcam pixel-Zarr home. |
| GWS internal archive | `/gws/ssde/j25b/gamb2le/data/internal/aurora-cloud` | Mirror manifests and internal archive evidence. |
| GWS SSHFS mount on droplet | `/mnt/gws/gamb2le` | Mount point used when a service writes directly to GWS. Required for the WXcam GWS-only Zarr policy. |

## Per-Instrument Paths

| Stream | Source path | Local raw mirror | Local product area | Main product paths |
| --- | --- | --- | --- | --- |
| CL61 ceilometer | `aurora@100.124.55.22:/home/aurora/data/cl61` | `/project/aurora/raw/cl61` | `/data/aurora/products/cl61` | `/data/aurora/products/cl61/gamb2le_depolarisation_lidar_ceilometer_aurora.zarr` |
| Cloud radar | `aurora@100.124.55.22:/home/aurora/data/rpgfmcw94` | `/project/aurora/raw/rpgfmcw94` | `/data/aurora/products/rpgfmcw94` | `/data/aurora/products/rpgfmcw94/cloud_radar.zarr` |
| HATPRO | `aurora@100.124.55.22:/home/aurora/data/hatprog5` | `/project/aurora/raw/hatprog5` | `/data/aurora/products/hatprog5` | `/data/aurora/products/hatprog5/hatpro.zarr`; quicklooks under `/data/aurora/products/quicklooks/hatpro` |
| Vaisala MET | `aurora@100.124.55.22:/home/aurora/data/vaisalamet` | `/project/aurora/raw/vaisalamet` | `/data/aurora/products/vaisalamet` | `/data/aurora/products/vaisalamet/vaisalamet.zarr` |
| ASFS logger | `aurora@100.124.55.22:/home/aurora/data/asfs/raw/crd` | `/project/aurora/raw/asfs` | `/data/aurora/products/asfs_logger` | `/data/aurora/products/asfs_logger/asfs_logger.zarr` |
| ASFS fast sonic | `aurora@100.124.55.22:/home/aurora/data/asfs/raw/crd` | `/project/aurora/raw/asfs` | `/data/aurora/products/asfs_fast_sonic` | `/data/aurora/products/asfs_fast_sonic/asfs_fast_sonic.zarr` |
| ASFS fast gas | `aurora@100.124.55.22:/home/aurora/data/asfs/raw/crd` | `/project/aurora/raw/asfs` | `/data/aurora/products/asfs_fast_gas` | `/data/aurora/products/asfs_fast_gas/asfs_fast_gas.zarr` |
| Power | `aurora@100.81.226.30:/data/power/level1` | `/project/aurora/raw/power/level1` | `/data/aurora/products/power` | `/data/aurora/products/power/power.zarr`; derived display stores under the same directory |
| Ops monitor | local collector output | `/project/aurora/raw/ops_monitor` | `/data/aurora/products/ops_monitor` | `/data/aurora/products/ops_monitor/ops_monitor.zarr`; health output under `/data/aurora/products/ops_monitor/health` |
| WXcam | `aurora@100.124.55.22:/home/aurora/data/wxcam` | `/project/aurora/raw/wxcam` | `/data/aurora/products/wxcam` | Catalog at `/data/aurora/products/wxcam/wxcam_catalog.sqlite`; daily MP4s under `/data/aurora/products/wxcam/daily_videos`; intended pixel Zarr at `/mnt/gws/gamb2le/data/output/aurora-cloud/products/wxcam/wxcam.zarr` |
| AURORACam / MX4 | `aurora@100.124.55.22:/home/aurora/data/mx4` | `/project/aurora/raw/auroracam` | `/data/aurora/products/auroracam` | Metadata Zarr at `/data/aurora/products/auroracam/auroracam.zarr`; JPEGs remain in the raw mirror |

## Active Zarr Inventory

These are the active Aurora Zarr products and their live time starts from the
2026-07-07 droplet check.

| Product | Path | Live first sample | Notes |
| --- | --- | --- | --- |
| CL61 ceilometer | `/data/aurora/products/cl61/gamb2le_depolarisation_lidar_ceilometer_aurora.zarr` | 2026-05-06T14:34:37 | Local product. |
| Cloud radar | `/data/aurora/products/rpgfmcw94/cloud_radar.zarr` | 2026-05-09T08:59:59 | Local product. |
| HATPRO | `/data/aurora/products/hatprog5/hatpro.zarr` | 2026-02-27T13:00:01 | Local product. |
| Vaisala MET | `/data/aurora/products/vaisalamet/vaisalamet.zarr` | 2026-05-02T00:00:42 | Local product. |
| ASFS logger | `/data/aurora/products/asfs_logger/asfs_logger.zarr` | 2026-05-02T00:00:00 | Local product. |
| ASFS fast sonic | `/data/aurora/products/asfs_fast_sonic/asfs_fast_sonic.zarr` | 2026-05-02T00:00:30 | Local product. |
| ASFS fast gas | `/data/aurora/products/asfs_fast_gas/asfs_fast_gas.zarr` | 2026-05-20T22:30:10 | Local product. |
| Power | `/data/aurora/products/power/power.zarr` | 2026-05-05T15:15:23 | Local product. |
| Power display summary | `/data/aurora/products/power/power_display_summary.zarr` | 2026-05-05T15:15:00 | Derived from the power Zarr for dashboard display. |
| Power display energy | `/data/aurora/products/power/power_display_energy.zarr` | 2026-05-05T15:15:00 | Legacy derived display store retained for compatibility. |
| Ops monitor | `/data/aurora/products/ops_monitor/ops_monitor.zarr` | 2026-05-09T16:01:00 | Local product. |
| WXcam fish HDR, current live store | `/data/aurora/products/wxcam/wxcam.zarr` group `fish_hdr` | 2026-05-02T00:00:00 | Old local store still present until the GWS-only rebuild is deployed. |
| WXcam pano HDR, current live store | `/data/aurora/products/wxcam/wxcam.zarr` group `pano_hdr` | 2026-01-12T02:25:00 | Old local store still present until the GWS-only rebuild is deployed. |
| WXcam fish HDR, intended store | `/mnt/gws/gamb2le/data/output/aurora-cloud/products/wxcam/wxcam.zarr` group `fish_hdr` | target start 2026-07-04T00:00:00Z | Configured target after deployment and rebuild. |
| WXcam pano HDR, intended store | `/mnt/gws/gamb2le/data/output/aurora-cloud/products/wxcam/wxcam.zarr` group `pano_hdr` | target start 2026-07-04T00:00:00Z | Configured target after deployment and rebuild. |
| AURORACam metadata | `/data/aurora/products/auroracam/auroracam.zarr` | 2026-07-07T11:47:00 | Metadata index only; image JPEGs are served from `/project/aurora/raw/auroracam`. |

The July 4 cutoff applies only to the WXcam pixel Zarr policy. Other active
Zarr products intentionally keep their current retained history unless a
separate rebuild policy is agreed.

## WXcam GWS-Only Policy

The configured target for the WXcam pixel Zarr is:

`/gws/ssde/j25b/gamb2le/data/output/aurora-cloud/products/wxcam/wxcam.zarr`

On the droplet this is addressed through:

`/mnt/gws/gamb2le/data/output/aurora-cloud/products/wxcam/wxcam.zarr`

The desired WXcam Zarr start time is:

`2026-07-04T00:00:00Z`

As of the 2026-07-07 live check, the old local WXcam Zarr still existed under
`/data/aurora/products/wxcam/wxcam.zarr`, and `/mnt/gws/gamb2le` was not mounted
on the droplet. The repo configuration is prepared for the GWS-only policy, but
the live service still needs deployment, the GWS mount, and a one-time rebuild
before the old local store can be removed.

## GWS Sync Jobs

The Aurora droplet pushes archive copies to GWS with these jobs:

| Job | Source | Destination | Schedule | Notes |
| --- | --- | --- | --- | --- |
| `raw` | `/project/aurora/raw/` | `/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/` | every 5 min | Mirrors raw instrument files. |
| `products` | `/data/aurora/products/` | `/gws/ssde/j25b/gamb2le/data/output/aurora-cloud/products/` | every 10 min | Excludes `/wxcam/**` so WXcam media cannot delay smaller products. |
| `products-wxcam` | `/data/aurora/products/wxcam/` | `/gws/ssde/j25b/gamb2le/data/output/aurora-cloud/products/wxcam/` | every 30 min | Excludes `wxcam.zarr` so the GWS-only Zarr is not deleted by product mirroring. |
| `manifests` | `/data/aurora/internal/mirror_manifests/` | `/gws/ssde/j25b/gamb2le/data/internal/aurora-cloud/manifests/` | every 10 min | Stores source/local/GWS verification evidence. |

GWS transfers use rsync over SSH through JASMIN transfer hosts:

1. `xfer-vm-03.jasmin.ac.uk`
2. `xfer-vm-01.jasmin.ac.uk`
3. `xfer-vm-02.jasmin.ac.uk`

## HTTP-Served Media

| Route | Filesystem backing path | Purpose |
| --- | --- | --- |
| `/wxcam-media` | `/data/aurora/products/wxcam` | WXcam daily MP4s and dashboard-facing media products. |
| `/auroracam-media` | `/project/aurora/raw/auroracam` | Full-resolution MX4 JPEG display. |

## Backup And Non-Active Zarr Directories

The droplet also has backup Zarr directories under product folders, including
cloud-radar and ops-monitor schema backups. These are not active dashboard
products and should not be treated as canonical data locations unless doing a
specific recovery.

## Related Iceflux Stack

The separate Iceflux deployment uses the same raw/product split, but under
Iceflux roots:

| Layer | Path |
| --- | --- |
| Iceflux raw root | `/project/iceflux/raw` |
| Iceflux product root | `/data/iceflux/products` |
| Iceflux ASFS Zarr | `/data/iceflux/products/asfs_logger/asfs_logger.zarr` |
| Iceflux fluxtower Zarr | `/data/iceflux/products/fluxtower/fluxtower_summary.zarr` |
| Iceflux app | `/opt/iceflux-cloud-dashboard` |
| Iceflux state | `/var/lib/iceflux-cloud` |
| Iceflux logs | `/var/log/iceflux-cloud` |
