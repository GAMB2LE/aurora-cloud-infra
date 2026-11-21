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

## Archives

Only this stream's filename family is checked, even when it shares a raw
folder with other streams. GWS and object copies are additive. ASS keeps raw
files for at least seven days; deletion needs an exact signed permit backed
by all three copies. A derived Zarr is not raw-retention proof. See
[ASS retention](ASS_BACKUP_AND_RETENTION.md).
