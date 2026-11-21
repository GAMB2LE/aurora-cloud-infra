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

## Access

Use the [source-access check](OPERATOR_QUICKSTART.md#source-access). These
syncs use Tailscale SSH without an installed source private key.

## Timers

`aurora-asfs-logger-source-sync.timer` runs
`/usr/local/bin/aurora-asfs-logger-sync`. The first run pulls all existing
matching CRD science files because `asfs_logger_source_start_fresh` is false.

Processing timers:

- `aurora-asfs-logger-append.timer`
- `aurora-asfs-logger-quicklooks.timer`

## Archives

Only this stream's filename family is checked, even when it shares a raw
folder with other streams. GWS and object copies are additive. ASS keeps raw
files for at least seven days; deletion needs an exact signed permit backed
by all three copies. A derived Zarr is not raw-retention proof. See
[ASS retention](ASS_BACKUP_AND_RETENTION.md).
