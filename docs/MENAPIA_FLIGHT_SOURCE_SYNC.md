# Menapia flight data

Production pulls corrected flight files from Menapia, keeps the original bytes,
and queues each exact path for the existing GWS and object-store writers.

```text
s3://menapia-flight-data-corrected/ (eu-west-1, read/list only)
  -> /project/aurora/raw/menapia/<source-key>
  -> /gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/menapia/<source-key>
  -> s3://gamb2le-o/data/incoming/aurora-cloud/raw/menapia/<source-key>
```

Keys normally follow `drone-uploads/YYYY/MM/DD/<dock>/<flight>/...`. The shared
bucket also contains other sites' test flights. Campaign classification stays
`unknown` until verified dock/flight mappings are configured; classification
changes metadata only.

## Ingest

`aurora-menapia-flight-source-sync.timer` runs every 15 minutes. It stays off
until `menapia_flight_credentials_commissioned=true` on production. Routine runs
handle up to 500 unseen versions, newest first. An explicitly reviewed initial
backfill can use `--max-objects 0` in a transient unit.

The ingest uses `rclone lsjson` and remote-to-local `copyto`. Its SQLite registry
is under `/var/lib/aurora-cloud/menapia-flight/`. Downloads are size-checked,
SHA-256 hashed and atomically published. The first content at a source key stays
canonical. Changed upstream bytes go to `_upstream_revisions/`; unsafe keys go
to `_unsafe_keys/`, with their original key retained in provenance.

## Credentials

Use `/etc/aurora-menapia/rclone.conf`, owned by root with mode `0600`:

```ini
[menapia]
type = s3
provider = AWS
access_key_id = <supplied separately>
secret_access_key = <supplied separately>
region = eu-west-1
```

Install from `menapia_flight_rclone_config_source` or the Vault value
`menapia_flight_rclone_config_content`; both tasks hide the value. A protected
preinstalled file also works. systemd `LoadCredential` supplies the private copy.
Keep keys out of Git, inventory, command lines, archives, logs and public status.

The old credential expired on 30 September 2026; the
[24 October record](records/2026-10-24-presentation-checks.md) reported
`AccessDenied`. That is historical evidence. Check today's inventory result and
configured expiry before claiming access works. The dashboard warns at 30 days
and turns red at seven days. Rotate by atomic file replacement, then run a
metadata-only inventory and one normal ingest. Keep the registry and raw data.

## Commissioning

Deploy with commissioning false, then run this on production:

```bash
sudo systemd-run --wait --collect \
  --unit=aurora-menapia-flight-inventory \
  --property=User=aurora --property=Group=aurora \
  --property=LoadCredential=menapia-rclone.conf:/etc/aurora-menapia/rclone.conf \
  /usr/local/bin/aurora-menapia-flight-sync \
  --config /etc/aurora-menapia/config.json --inventory-only
```

Check authentication, object/byte counts, hierarchy warnings and latest flight
in `/data/aurora/internal/archive_status/menapia-flight-source-sync.json`.
Replace `--inventory-only` with `--include-key <exact-source-key>` for one sample.
Require the local byte count/SHA, a queued `menapia/...` path, independent GWS
and object-store presence, and an idempotent second ingest. Then enable the
commissioned flag, apply the source/archive/monitor roles and start the timer.

## Display products

`aurora-menapia-flight-products.timer` reads canonical `data_files/` bundles
every 30 minutes on production. A complete flight needs Drone/DRN, SN0122 and
SN0123 streams. M350-only, incomplete, revision and unsafe-key bundles stay out
of public products. The campaign start defaults to 25 August 2026; review scope
before changing `aurora_menapia_product_campaign_start_day`.

Valid CSV takes precedence over legacy binary input. Samples are limited to
the path's UTC date and the valid drone-altitude interval. Products use exact
one-second medians, visible null gaps and no interpolation. Units are Celsius,
hPa, percent RH and metres; quality warnings record rejected physical bounds.

Outputs under `/data/aurora/products/` are:

| Path | Contents |
| --- | --- |
| `menapia/catalog.json` | Schema 1, newest days first, flight metadata and relative detail/plot paths. |
| `menapia/flights/<stable-path-hash>.json` | Equal-length time, temperature, pressure, humidity and altitude arrays. |
| `menapia/plots/<stable-path-hash>.png` | Four flight panels. |
| `quicklooks/uas/uas__summary__YYYYMMDD.png` | All decoded flights for that UTC day. |
| `quicklooks/uas/uas__summary__latest.png` | Atomic copy of the newest complete daily image. |

A changed day is staged before publication. Decode/render failure retains the
previous complete day, reports `partial_failure` and leaves its fingerprint
uncommitted. Unchanged runs do not rewrite artifacts.

```bash
uv run ansible-playbook playbooks/menapia_products.yml --limit aurora-cloud --check --diff
uv run ansible-playbook playbooks/menapia_products.yml --limit aurora-cloud
```

This focused release installs only the headless producer. It has no network
access and writes only its product, quicklook and state roots. Generic product
archive jobs and the `product-menapia`/`product-quicklooks` mirror stages carry
the outputs; development does not build them.

## Diagnose a failure

Read the source/product service journals and the ingest JSON above. The product
summary is `/data/aurora/products/menapia/catalog.json`; archive health exposes
`.source_ingest.menapia` in `health-v1.json`.

- Authentication: rotate/extend the credential and retry inventory first.
- Partial ingest: inspect the sanitized object error; committed files stay put.
- Archive pending: inspect `aurora-archive-dispatch.service` and its normal writers.
- Upstream change: inspect `_upstream_revisions` and the provenance manifest.

Run manifests live under `/data/aurora/internal/menapia-flight/manifests/` and
are archived under the corresponding `data/internal/aurora-cloud/menapia-flight/`
roots on GWS and `gamb2le-o`.
