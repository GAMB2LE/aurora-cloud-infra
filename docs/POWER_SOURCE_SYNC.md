# Power Source Sync

- Source: `aurora@100.81.226.30:/data/power/level1`
- Target raw directory: `/project/aurora/raw/power/level1`
- Target Zarr: `/data/aurora/products/power/power.zarr`
- Quicklooks: `/data/aurora/products/quicklooks/power`

Only files matching `power_data_YYYYMMDD.csv` are synced. The parser writes all
numeric variables into one time-indexed Zarr, excluding any column whose name
contains `wind` case-insensitively.

The source sync intentionally rescans a rolling ten-day window on each run.
That catches in-place updates to recent daily CSV files instead of relying only
on a single monotonic timestamp cursor.

The dashboard exposes this as its own instrument selection, `power`, with
stacked 1D time-series plots for every retained numeric variable.

## Authentication

The sync uses Tailscale SSH over the tailnet. The rsync remote shell is regular
`ssh` with identity keys disabled:

- `IdentitiesOnly=yes`
- `IdentityFile=none`
- `PubkeyAuthentication=no`
- `StrictHostKeyChecking=accept-new`

No private key is installed for this source.

## Timers

`aurora-power-source-sync.timer` runs `/usr/local/bin/aurora-power-sync`. The
first run pulls all existing matching files because `power_source_start_fresh`
is false.

Processing timers:

- `aurora-power-append.timer`
- `aurora-power-quicklooks.timer`
