# ASFS Fast-Gas Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/asfs/raw/crd`
- Target raw directory: `/project/aurora/raw/asfs/crd`
- Target Zarr: `/data/aurora/products/asfs_fast_gas/asfs_fast_gas.zarr`

Current source files are chunked Campbell CRD TOA5 files matching
`aurora_asfs_data_fast_gas_YYYYMMDDHHMM.dat`. The CRD mirror accepts files at
or after `202605020000` and rescans a rolling ten-day window so recent in-place
updates are recopied.

## Authentication

The sync uses Tailscale SSH with private-key authentication disabled. No source
private key is installed.

## Timers

- `aurora-asfs-fast-gas-source-sync.timer`
- `aurora-asfs-fast-gas-append.timer`

## Archives

Only this stream's filename family is checked, even when it shares a raw
folder with other streams. GWS and object copies are additive. ASS keeps raw
files for at least seven days; deletion needs an exact signed permit backed
by all three copies. A derived Zarr is not raw-retention proof. See
[ASS retention](ASS_BACKUP_AND_RETENTION.md).
