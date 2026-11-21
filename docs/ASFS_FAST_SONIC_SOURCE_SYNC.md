# ASFS Fast-Sonic Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/asfs/raw/crd`
- Target raw directory: `/project/aurora/raw/asfs/crd`
- Target Zarr: `/data/aurora/products/asfs_fast_sonic/asfs_fast_sonic.zarr`

Current source files are chunked Campbell CRD TOA5 files matching
`aurora_asfs_data_fast_sonic_YYYYMMDDHHMM.dat`. Historical files under
`/project/aurora/raw/asfs/loggernet` remain supported by the appender, so the
Zarr can span the older `asfs-logger_fast_sonic_DD_MM_YYYY.dat` files and the
newer CRD chunks. No dashboard science or housekeeping quicklook is configured
for this pipeline.

The CRD mirror is limited to files with timestamps at or after
`202605020000`, matching the May 2 onward data-retention reset.

The parser reads Campbell TOA5 files by using the second line as the column
header and skipping the unit/process rows. The Zarr time coordinate is built
from `TIMESTAMP + metek_msec_out` so sub-second fast-sonic samples are
preserved.

The source sync intentionally rescans a rolling ten-day window on each run.
That catches in-place updates to recent CRD chunks instead of relying only on a
single monotonic timestamp cursor.

## Access

Use the [source-access check](OPERATOR_QUICKSTART.md#source-access). These
syncs use Tailscale SSH without an installed source private key.

## Timers

`aurora-asfs-fast-sonic-source-sync.timer` runs
`/usr/local/bin/aurora-asfs-fast-sonic-sync`. The first run pulls all existing
matching CRD fast-sonic files because `asfs_fast_sonic_source_start_fresh` is
false.

Processing timer:

- `aurora-asfs-fast-sonic-append.timer`

## Archives

Only this stream's filename family is checked, even when it shares a raw
folder with other streams. GWS and object copies are additive. ASS keeps raw
files for at least seven days; deletion needs an exact signed permit backed
by all three copies. A derived Zarr is not raw-retention proof. See
[ASS retention](ASS_BACKUP_AND_RETENTION.md).
