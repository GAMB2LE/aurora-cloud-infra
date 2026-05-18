# ASFS Science Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/asfs/raw/crd`
- Target raw directory: `/project/aurora/raw/asfs/crd`
- Target Zarr: `/data/aurora/products/asfs_logger/asfs_logger.zarr`
- Quicklooks: `/data/aurora/products/quicklooks/asfs_logger`

Current source files are chunked Campbell CRD TOA5 files matching
`aurora_asfs_data_sci_YYYYMMDDHHMM.dat`. Historical files under
`/project/aurora/raw/asfs/loggernet` are still supported by the appender, so
the Zarr can span the older `asfs-logger_sci_DD_MM_YYYY.dat` files and the
newer CRD chunks. The parser reads Campbell TOA5 files by using the second line
as the column header and skipping the unit/process rows.

The CRD mirror is limited to files with timestamps at or after
`202605020000`, matching the May 2 onward data-retention reset.

The source sync intentionally rescans a rolling ten-day window on each run.
That catches in-place updates to recent CRD chunks instead of relying only on a
single monotonic timestamp cursor.

The dashboard presents this store through the curated **Radiation** instrument
and through `HK_ASFS` housekeeping quicklooks. The raw `asfs-logger` variable
picker is not the normal deployed user-facing view.

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
matching CRD science files because `asfs_logger_source_start_fresh` is false.

Processing timers:

- `aurora-asfs-logger-append.timer`
- `aurora-asfs-logger-quicklooks.timer`
