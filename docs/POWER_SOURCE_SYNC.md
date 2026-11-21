# APS power source sync

| Data | Path |
| --- | --- |
| Source | `aurora@100.81.226.30:/data/power/level1` |
| Production raw | `/project/aurora/raw/power/level1` |
| Product | `/data/aurora/products/power/power.zarr` |
| Quicklooks | `/data/aurora/products/quicklooks/power` |

Only `power_data_YYYYMMDD.csv` is copied. The product keeps numeric variables
except columns whose names contain `wind`, ignoring case. The dashboard calls
this instrument **Aurora Power Supply**.

The live scan revisits ten days to catch daily files updated in place.
`power_source_mtime_grace_seconds` allows small source-clock differences;
changed size or mtime causes another copy. With `power_source_start_fresh:
false`, an absent cursor includes existing matching history.

Use the [source-access check](OPERATOR_QUICKSTART.md#source-access).
Connection setup is limited to one 15-second attempt, with 15-second keepalives
and two missed replies. The service has a 90-second outer limit. A failed run
records failure and leaves the next two-minute tick to retry.

## Timers and checks

- `aurora-power-source-sync.timer` calls `/usr/local/bin/aurora-power-sync`.
- `aurora-power-append.timer` appends the product.
- `aurora-power-quicklooks.timer` updates plots.

I check the newest source sample and product frontier, not just the timer.
Raw and derived files are archived separately to GWS and object storage.
**APS Power is non-prunable**: no cloud permit can delete `/data/power/level1`.
Changing that needs a separate cloud-and-edge policy review. See
[ASS retention](ASS_BACKUP_AND_RETENTION.md).
