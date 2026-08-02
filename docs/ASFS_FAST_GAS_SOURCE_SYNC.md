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

## Backup and retention

Only the fast-gas filename family is verified for this stream, although it
shares `/asfs/crd` with science and fast-sonic files. Matching raw files are
archived additively to GWS and object storage and kept on ASS for at least seven
days. Deletion requires an exact signed permit backed by cloud, GWS, and
object-store proof. The derived Zarr is not raw-retention evidence. See
[Backups and Archive Services](ARCHIVE_SERVICES.md).
