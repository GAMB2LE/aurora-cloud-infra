# Menapia Flight Data Source Sync

## Purpose and data flow

`aurora-menapia-flight-source-sync.service` pulls immutable corrected flight
objects from the Menapia-managed AWS bucket. Production AURORA Cloud is the
only ingest writer. It lands raw files locally and enqueues those exact paths
into the existing independent GWS and object-store archive dispatcher.

```text
s3://menapia-flight-data-corrected/ (eu-west-1, read/list only)
  -> /project/aurora/raw/menapia/<source-key>
  -> /gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/menapia/<source-key>
  -> s3://gamb2le-o/data/incoming/aurora-cloud/raw/menapia/<source-key>
```

Normal source keys currently follow:

```text
drone-uploads/YYYY/MM/DD/<dock-id>/<flight-id>/...
```

The bucket is shared with Menapia test flights from other sites. No reliable
campaign mapping has yet been supplied, so the ingest preserves every object
and records its campaign classification as `unknown`. Configure
`menapia_flight_campaign_dock_ids` or `menapia_flight_campaign_flight_ids`
only after the mapping has been verified. Classification changes metadata; it
does not move or delete raw files.

## Scheduling and immutability

The systemd timer runs every 15 minutes with a bounded randomized delay. The
timer is installed but remains disabled until
`menapia_flight_credentials_commissioned` is set to `true` on the production
writer after the initial inventory and sample checks.

The ingest uses only `rclone lsjson` and remote-to-local `rclone copyto`.
It never sends upload, move, sync-delete, or delete requests to Menapia. A
SQLite registry under `/var/lib/aurora-cloud/menapia-flight/` tracks source
fingerprints and archive-enqueue state. Downloads are size-checked, SHA-256
hashed, and atomically published.

The first received content for a source key remains at its canonical path. If
the upstream fingerprint and bytes later change, the new bytes are retained
under `raw/menapia/_upstream_revisions/`; the original is not overwritten.
Unsafe filesystem keys are retained under `raw/menapia/_unsafe_keys/`, with
their original S3 key recorded in provenance.

## Flight display products

The separate `aurora-menapia-flight-products.timer` runs on the authoritative
production writer every 30 minutes. It is a read-only consumer of canonical
bundles below:

```text
/project/aurora/raw/menapia/drone-uploads/YYYY/MM/DD/<dock>/<flight>/data_files/
```

A bundle qualifies only when it contains Drone/DRN, SN0122, and SN0123
streams. M350-only bundles, incomplete bundles, `_upstream_revisions`, and
`_unsafe_keys` do not become public flight products. CSV is preferred when it
is valid; the original binary stream is the fallback for legacy flights. The
campaign product boundary defaults to 25 August 2026; earlier shared-bucket
test bundles are excluded before completeness and deferred-bundle accounting.
Change `aurora_menapia_product_campaign_start_day` only after campaign scope is
reviewed.
Values are filtered to the UTC date encoded in the canonical path and then to
the first-to-last valid drone-altitude second. Products contain exact
one-second medians with explicit null gaps and no interpolation. Temperature
is degrees Celsius, pressure is converted from Pa to hPa, relative humidity is
percent, and fused drone altitude is metres. Physical bounds exclude corrupt
values and are recorded in the flight quality warnings.

The public-safe product contract is:

```text
/data/aurora/products/menapia/catalog.json
/data/aurora/products/menapia/flights/<stable-path-hash>.json
/data/aurora/products/menapia/plots/<stable-path-hash>.png
/data/aurora/products/quicklooks/uas/uas__summary__YYYYMMDD.png
/data/aurora/products/quicklooks/uas/uas__summary__latest.png
```

`catalog.json` is schema version 1 and lists days newest first plus flight
metadata and relative detail/plot filenames. Each detail JSON holds equal
length, columnar `timeUTC`, temperature, pressure, humidity, and altitude
arrays. A per-flight PNG has the same four panels. The dated UAS science
quicklook overlays every decoded flight for its UTC day; `latest` is an atomic
copy of the newest complete dated image. Sparse legacy pressure values have
visible point markers, while null seconds remain line breaks.

Publication is day-transactional. Changed JSON, per-flight PNGs, and the dated
all-flights image are staged before any of them replaces a published artifact.
If decode or rendering fails, the previous complete day and catalog entries
remain available, the catalog reports `partial_failure`, and the input
fingerprint is not committed. Repeated successful runs do not rewrite
unchanged flight or quicklook artifacts.

Deploy only this headless production producer with the focused playbook:

```bash
uv run ansible-playbook playbooks/menapia_products.yml --limit aurora-cloud --check --diff
uv run ansible-playbook playbooks/menapia_products.yml --limit aurora-cloud
```

This playbook does not apply or restart the dashboard, mobile API, source
ingest, or archive services. Its systemd unit has no network access, reads the
raw tree read-only, and can write only its product, quicklook, and state roots.
The existing generic `products` GWS/object-store jobs archive these rebuildable
files; no Menapia-specific archive writer is added. The standby replication
stages `product-menapia` and `product-quicklooks` mirror them to data-ocean,
which never runs the product builder itself.

## Credentials and rotation

The root-owned rclone profile is:

```text
/etc/aurora-menapia/rclone.conf
```

It must be `root:root` mode `0600`. systemd `LoadCredential` gives the running
service a private read-only copy. The key is never placed in Git, Ansible
inventory, the command line, GWS, logs, status JSON, or dashboard responses.

Prepare the file outside this repository with this structure:

```ini
[menapia]
type = s3
provider = AWS
access_key_id = <supplied separately>
secret_access_key = <supplied separately>
region = eu-west-1
```

Install it from a protected controller file by setting
`menapia_flight_rclone_config_source`, or through an Ansible Vault value in
`menapia_flight_rclone_config_content`. Both tasks use `no_log`. A preinstalled
file is also supported.

The current pair is recorded as valid through 30 September 2026. The dashboard
warns at 30 days and becomes red at seven days. Because the exact cutoff time
is unknown, replace it no later than 29 September. Rotation is an atomic file
replacement followed by a metadata-only inventory and one normal service run;
the database and raw data remain unchanged.

## Initial inventory and commissioning

Deploy with commissioning still false. On production, run a read-only
inventory through a transient systemd unit so the credential is not exposed:

```bash
sudo systemd-run --wait --collect \
  --unit=aurora-menapia-flight-inventory \
  --property=User=aurora --property=Group=aurora \
  --property=LoadCredential=menapia-rclone.conf:/etc/aurora-menapia/rclone.conf \
  /usr/local/bin/aurora-menapia-flight-sync \
  --config /etc/aurora-menapia/config.json --inventory-only
```

Review `/data/aurora/internal/archive_status/menapia-flight-source-sync.json`
for `upstream_objects_examined`, `upstream_bytes_examined`, bounded
`source_path_samples`, hierarchy counts/warnings, latest date/flight, and
authentication status. Then ingest one representative key by replacing `--inventory-only`
with `--include-key <exact-source-key>`. Verify:

1. exact local byte count and SHA-256 provenance;
2. a queued `menapia/...` path in the shared archive dispatcher;
3. the same relative path on GWS and in `gamb2le-o`;
4. a second run performs no download and creates no duplicate.

After review, set `menapia_flight_credentials_commissioned=true`, apply the
`source_sync`, `gws_sync`, `object_store_mirror`, and `operations_monitor`
roles, and start the timer. Use `--max-objects 0` in a transient unit for an
unbounded initial backfill; routine timer runs process newest objects first and
advance up to 500 unseen versions per run.

## Status and troubleshooting

Useful checks are:

```bash
systemctl status aurora-menapia-flight-source-sync.timer
systemctl status aurora-menapia-flight-source-sync.service
journalctl -u aurora-menapia-flight-source-sync.service --since today
jq . /data/aurora/internal/archive_status/menapia-flight-source-sync.json
jq '.source_ingest.menapia' /data/aurora/internal/archive_status/health-v1.json
systemctl status aurora-menapia-flight-products.timer
journalctl -u aurora-menapia-flight-products.service --since today
jq '{lastRunState, latestFlightID, availableDays}' /data/aurora/products/menapia/catalog.json
```

The health contract and UAS dashboard show the last attempt and success, source
objects examined, new objects and bytes, failures, authentication state,
unclassified objects, latest flight/date, credential lifetime, and Menapia-only
GWS/object-store delivery counts.

- Authentication failure: replace or extend the Menapia credential, run an
  inventory-only check, then retry the service.
- Partial failure: inspect the sanitized per-object errors. Successful files
  remain committed; the next run retries only missing versions.
- GWS or object-store pending: inspect `aurora-archive-dispatch.service` and
  the corresponding existing archive writer. Source ingestion does not create
  an alternative delivery path.
- Changed upstream object: review `_upstream_revisions` and the matching
  internal provenance manifest before any downstream processing decision.

Durable per-run provenance is stored locally under
`/data/aurora/internal/menapia-flight/manifests/` and copied through the normal
internal archive jobs to:

```text
/gws/ssde/j25b/gamb2le/data/internal/aurora-cloud/menapia-flight/manifests/
s3://gamb2le-o/data/internal/aurora-cloud/menapia-flight/manifests/
```
