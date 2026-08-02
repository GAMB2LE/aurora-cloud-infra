# ASS PDU Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/pdu`
- Target raw directory: `/project/aurora/raw/pdu`
- Target Zarr: `/data/aurora/products/power/pdu.zarr`

The source sync copies flat daily files matching `pdu_DDMMYYYY.csv`. It scans a
rolling ten-day window and permits a five-minute source-clock grace so the
active daily file is recopied when its size or mtime changes.

## Authentication

The sync uses Tailscale SSH with private-key authentication disabled. No source
private key is installed.

## Timers

- `aurora-pdu-source-sync.timer`
- `aurora-pdu-append.timer`

## Backup and retention

Matching PDU CSV files are archived additively to GWS and object storage. ASS
keeps each file for at least seven days; deletion requires an unchanged exact
path in a signed permit backed by cloud, GWS, and object-store proof. The
derived `pdu.zarr` is a product and is not raw-retention evidence. See
[Backups and Archive Services](ARCHIVE_SERVICES.md).
