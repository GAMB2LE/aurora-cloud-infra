# ASFS LoggerNet Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/asfs/raw/loggernet`
- Target raw directory: `/project/aurora/raw/asfs/loggernet`
- Target Zarr: `/data/aurora/products/asfs_logger/asfs_logger.zarr`
- Quicklooks: `/data/aurora/products/quicklooks/asfs_logger`

Only flat source files matching `asfs-logger_sci_DD_MM_YYYY.dat` are synced.
The parser reads Campbell TOA5 files by using the second line as the column
header and skipping the unit/process rows.

The dashboard exposes this as its own instrument selection, `asfs-logger`, with
stacked 1D time-series plots for every numeric variable.

## Authentication

The sync uses Tailscale SSH over the tailnet. The rsync remote shell is regular
`ssh` with identity keys disabled:

- `IdentitiesOnly=yes`
- `IdentityFile=none`
- `PubkeyAuthentication=no`
- `StrictHostKeyChecking=accept-new`

No private key is installed for this source.

## Timers

`aurora-asfs-logger-source-sync.timer` runs
`/usr/local/bin/aurora-asfs-logger-sync`. The first run pulls all existing
matching files because `asfs_logger_source_start_fresh` is false.

Processing timers:

- `aurora-asfs-logger-append.timer`
- `aurora-asfs-logger-quicklooks.timer`
