# Aurora Data Locations

This page describes the configured storage contract across the source hosts,
production, development, and the GWS archive. The Ansible inventory is the
source of truth for configured paths. Use the Operations Dashboard and current
host checks for live availability, capacity, or time coverage.

## Storage Roots

| Layer | Path or host | Purpose |
| --- | --- | --- |
| ASS Linux source | `aurora@100.124.55.22:/home/aurora/data` | Main Aurora source host for CL61, radar, HATPRO, Vaisala MET, ASFS, WXcam, and MX4/AURORACam ingest. |
| APS Linux source | `aurora@100.81.226.30:/data/power/level1` | Aurora Power Supply level-1 source files. |
| Local raw mirror | `/project/aurora/raw` | Authoritative production raw mirror. Development receives a live mirror. Product rebuilds start here, not from GWS. |
| Local products | `/data/aurora/products` | Dashboard-serving products: Zarrs, quicklooks, catalogs, daily videos, health reports, and small derived stores. |
| Local state | `/var/lib/aurora-cloud` | Sync markers, append state, lock files, and operational state. |
| Local logs | `/var/log/aurora-cloud` | Service logs for source syncs, appenders, quicklooks, and GWS jobs. |
| Dashboard app | `/opt/aurora-cloud-dashboard` | Deployed dashboard code and virtualenv. |
| GWS raw archive | `/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw` | Archived copy of `/project/aurora/raw`. |
| GWS product archive | `/gws/ssde/j25b/gamb2le/data/output/aurora-cloud/products` | Archived copy of selected dashboard products. |
| GWS internal archive | `/gws/ssde/j25b/gamb2le/data/internal/aurora-cloud` | Mirror manifests and internal archive evidence. |

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
| WXcam | `aurora@100.124.55.22:/home/aurora/data/wxcam` | `/project/aurora/raw/wxcam` | `/data/aurora/products/wxcam` | Catalog at `/data/aurora/products/wxcam/wxcam_catalog.sqlite`; daily MP4s under `/data/aurora/products/wxcam/daily_videos`. The dashboard does not require a WXcam pixel Zarr. |
| AURORACam / MX4 | `aurora@100.124.55.22:/home/aurora/data/mx4` | `/project/aurora/raw/auroracam` | `/data/aurora/products/auroracam` | Metadata Zarr at `/data/aurora/products/auroracam/auroracam.zarr`; JPEGs remain in the raw mirror |

## GWS Sync Jobs

Production archives raw data, selected products, and mirror manifests to GWS.
Development receives its live data from production and does not own archive
writes. Exact enabled timers and current archive coverage are operational state;
check them on the production host or in Operations.

| Job | Source | Destination | Schedule | Notes |
| --- | --- | --- | --- | --- |
| `raw` | `/project/aurora/raw/` | `/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/` | Raw archive evidence. |
| `products` | `/data/aurora/products/` | `/gws/ssde/j25b/gamb2le/data/output/aurora-cloud/products/` | Product archive evidence. |
| `manifests` | `/data/aurora/internal/mirror_manifests/` | `/gws/ssde/j25b/gamb2le/data/internal/aurora-cloud/manifests/` | Source/local/archive verification evidence. |

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
