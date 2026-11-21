# Vaisala MET source sync

| Data | Path |
| --- | --- |
| Source | `aurora@100.124.55.22:/home/aurora/data/vaisalamet` |
| Production raw | `/project/aurora/raw/vaisalamet` |
| Product | `/data/aurora/products/vaisalamet/vaisalamet.zarr` |
| Quicklooks | `/data/aurora/products/quicklooks/vaisalamet` |

The source writes daily `vaisala_met_level0_DD-MM-YYYY.dat` files. The parser
keeps numeric columns as one-dimensional time series in one Zarr.

`aurora-vaisalamet-source-sync.timer` runs `/usr/local/bin/aurora-vaisalamet-sync`.
With `vaisalamet_source_start_fresh: false`, an absent cursor includes existing
history. The current file keeps growing, so selection has no fixed upper-mtime
cutoff. Each run revisits five minutes and queues the exact copied paths for
both archives.

Use the [source-access check](OPERATOR_QUICKSTART.md#source-access).
Processing uses `aurora-vaisalamet-append.timer` and
`aurora-vaisalamet-quicklooks.timer`. I compare actual source and product sample
times before calling a stalled stream recovered.

Raw files reach GWS and object storage additively. ASS keeps them for at least
seven days; signed exact-file proof is required for deletion. The product is
not raw-retention proof. See [ASS retention](ASS_BACKUP_AND_RETENTION.md).
