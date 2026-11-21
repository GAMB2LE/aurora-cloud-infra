# CL61 source sync

I check the newest ASS file, its production copy and the decoded product time.
A recent sync exit alone does not show that new observations arrived.

| Data | Path |
| --- | --- |
| Source | `aurora@100.124.55.22:/home/aurora/data/cl61` |
| Production raw | `/project/aurora/raw/cl61` |
| Product | `/data/aurora/products/cl61/gamb2le_depolarisation_lidar_ceilometer_aurora.zarr` |
| Quicklooks | `/data/aurora/products/quicklooks/ceilometer` |

## How it runs

`aurora-cl61-source-sync.timer` calls `/usr/local/bin/aurora-cl61-sync`.
With `cl61_source_start_fresh: false`, an absent cursor starts from the available
source history. `start_fresh: true` instead records a new baseline and skips
older files. Keep that choice explicit; it changes which history is selected.

The active source uses [Tailscale SSH](OPERATOR_QUICKSTART.md#source-access).
The old `celine-edge-1` host is retired. The optional `ssh_key` authentication
mode remains for legacy configurations, not as a reason to resume that source.

Processing uses:

- `aurora-ceilometer-append.timer`
- `aurora-ceilometer-last24h.timer`
- `aurora-ceilometer-quicklooks.timer`

## If data stop advancing

Check source timestamps, sync/append journals and the product's actual time
coordinate. Preserve the cursor during gap recovery. Before any deliberate
baseline reset, stop new timer starts, let the service finish and save its
state. Record which history would be skipped before changing it.

Do not revive the retired source push to `/mnt/data/cl61`: it used its own
delete policy and was disabled in May 2026. The current path is pull, archive,
independent verification and signed retention.

Raw NetCDF files are copied to GWS and object storage. ASS keeps them for at
least seven days. Zarrs and quicklooks do not replace exact raw-file proof.
See [ASS retention](ASS_BACKUP_AND_RETENTION.md).
