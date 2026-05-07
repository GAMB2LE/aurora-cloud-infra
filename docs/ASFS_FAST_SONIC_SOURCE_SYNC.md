# ASFS Fast-Sonic Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/asfs/raw/loggernet`
- Target raw directory: `/project/aurora/raw/asfs/loggernet`
- Target Zarr: `/data/aurora/products/asfs_fast_sonic/asfs_fast_sonic.zarr`

Only flat source files matching `asfs-logger_fast_sonic_DD_MM_YYYY.dat` are
synced. No dashboard instrument or quicklook generation is configured for this
pipeline.

The parser reads Campbell TOA5 files by using the second line as the column
header and skipping the unit/process rows. The Zarr time coordinate is built
from `TIMESTAMP + metek_msec_out` so sub-second fast-sonic samples are
preserved.

## Authentication

The sync uses Tailscale SSH over the tailnet. The rsync remote shell is regular
`ssh` with identity keys disabled:

- `IdentitiesOnly=yes`
- `IdentityFile=none`
- `PubkeyAuthentication=no`
- `StrictHostKeyChecking=accept-new`

No private key is installed for this source.

## Timers

`aurora-asfs-fast-sonic-source-sync.timer` runs
`/usr/local/bin/aurora-asfs-fast-sonic-sync`. The first run pulls all existing
matching files because `asfs_fast_sonic_source_start_fresh` is false.

Processing timer:

- `aurora-asfs-fast-sonic-append.timer`
