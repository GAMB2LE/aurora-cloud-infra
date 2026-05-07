# Vaisala Met Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/vaisalamet`
- Target raw directory: `/project/aurora/raw/vaisalamet`
- Target Zarr: `/data/aurora/products/vaisalamet/vaisalamet.zarr`
- Quicklooks: `/data/aurora/products/quicklooks/vaisalamet`

The source stores flat CSV-style `.dat` files named
`vaisala_met_level0_DD-MM-YYYY.dat`. The dashboard pipeline parses every numeric
column as a 1D time series and stores all variables in a single time-indexed
Zarr.

## Authentication

The sync uses Tailscale SSH over the tailnet. The rsync remote shell is regular
`ssh` with identity keys disabled:

- `IdentitiesOnly=yes`
- `IdentityFile=none`
- `PubkeyAuthentication=no`
- `StrictHostKeyChecking=accept-new`

No private key is installed for this source.

## Timers

`aurora-vaisalamet-source-sync.timer` runs
`/usr/local/bin/aurora-vaisalamet-sync`. The first run pulls all existing
matching `.dat` files because `vaisalamet_source_start_fresh` is false.

Processing timers:

- `aurora-vaisalamet-append.timer`
- `aurora-vaisalamet-quicklooks.timer`
