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

APS writes the current UTC-day CSV in place. The source sync therefore allows a
small upper mtime grace window, controlled by
`power_source_mtime_grace_seconds`, when listing files on the source host. This
prevents the active file from being skipped because its fractional mtime lands
just after the sync process sampled its own `now` timestamp. The next rolling
scan recopies the file if its size or mtime changes.

The dashboard exposes this as **Aurora Power Supply**, with curated science and
housekeeping plots rather than a freeform plot of every retained numeric
variable.

## Authentication

The sync uses Tailscale SSH over the tailnet. The rsync remote shell is regular
`ssh` with identity keys disabled:

- `IdentitiesOnly=yes`
- `IdentityFile=none`
- `PubkeyAuthentication=no`
- `StrictHostKeyChecking=accept-new`

No private key is installed for this source.

Connection establishment is bounded to one 15-second attempt. Established SSH
and rsync sessions use 15-second keepalives and fail after two unanswered
keepalives; systemd caps the entire one-shot sync at 90 seconds. A broken APS
overlay path therefore records a failed attempt and yields to the next
two-minute timer run instead of occupying the service indefinitely.

## Timers

`aurora-power-source-sync.timer` runs `/usr/local/bin/aurora-power-sync`. The
first run pulls all existing matching files because `power_source_start_fresh`
is false.

Processing timers:

- `aurora-power-append.timer`
- `aurora-power-quicklooks.timer`

## Backup and retention

Matching APS Power CSV files are copied additively to GWS and object storage,
and the derived Power products are archived separately. APS is explicitly
non-prunable: no cloud permit can authorize deletion from
`/data/power/level1`, regardless of archive health. Enabling APS retention
requires a separate policy review in both cloud and edge infrastructure repos.
See [Backups and Archive Services](ARCHIVE_SERVICES.md).
