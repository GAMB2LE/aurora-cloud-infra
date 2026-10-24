# BLE enclosure temperature and humidity

The `blesensors` stream receives the APS and ASS SwitchBot measurements from
the collector on ASS. It is independent of the scientific surface meteorology
and the `ops_monitor` product. No email or notification rules are introduced.

- Field source: `/home/aurora/data/blesensors/`, CSV files plus legacy
  `ble_temperatures_*.log` files, preserved without rewriting.
- Production raw mirror: `/project/aurora/raw/blesensors/`.
- Product: `/data/aurora/products/blesensors/blesensors.zarr`.
- Source and append timers run every two minutes on the authoritative writer.
  Development normally consumes the production mirror; do not change writer
  authority merely because production is temporarily unreachable.
- Source sync uses the existing ASS transport variables, bounded connections,
  additive rsync, and the raw archive dispatcher. The first run imports all
  matching available history; subsequent runs revisit modified files across
  a ten-day overlap. Legacy filename dates must not restrict sample parsing.
- CSV takes precedence for each sensor and UTC second where both formats
  contain readings. Legacy logs fill the remaining history and CSV outages;
  repeated imports and late CSV arrivals do not duplicate those observations.
- `BLESENSORS_DIR`, `BLESENSORS_ZARR_PATH`, and `BLESENSORS_SOURCE_LABEL`
  configure the application. The normal source label is `SwitchBot via ASS`.

## Focused production release

Deploy the reviewed dashboard/appender code first, then use the focused
playbook. It changes only BLE units, directories, three environment entries,
and the BLE entries in the existing archive bindings. It does not restart the
dashboard, API, archive workers, or unrelated source/processing services.

```bash
uv run ansible-playbook playbooks/blesensors.yml --syntax-check
uv run ansible-playbook playbooks/blesensors.yml --limit aurora-cloud --check --diff
uv run ansible-playbook playbooks/blesensors.yml --limit aurora-cloud
```

The focused archive updater retains the live verifier and archive-health code,
all other streams, object-store job definitions, and recovery configuration.
It validates all five deployed targets before writing and preserves ownership/mode.
Backups are reported under `/var/backups/aurora-blesensors/<release>/`; repeat
runs are idempotent. A missing or unrecognised deployed binding fails closed.
The standard managed templates also contain BLE for future complete rebuilds.
Pruning remains disabled for BLE, including when the full retention stack is
subsequently reapplied.

Optional tags are `blesensors_source`, `blesensors_processing`,
`blesensors_archive`, and `blesensors_standby`. For staged installation before
starting a writer, override `blesensors_source_sync_timer_enabled=false` and
`aurora_processing_timers={"blesensors_append":false}` for this focused playbook
only, then remove those overrides for activation. This small override dictionary
must not be used with a broad runtime release.

## Development mirror

The regular standby stage lists include `raw-blesensors` and
`product-blesensors`. To add just those stages on an existing development host:

```bash
uv run ansible-playbook playbooks/blesensors.yml --limit aurora-cloud-droplet --check --diff -e blesensors_configure_standby=true
uv run ansible-playbook playbooks/blesensors.yml --limit aurora-cloud-droplet -e blesensors_configure_standby=true
```

The focused mirror uses `/usr/local/bin/aurora-blesensors-mirror` and a dedicated
service, leaving other deployed mirror/continuity scripts unchanged. It installs
only the two BLE timers and performs additive transfers. Its state and freshness
stamp live under separate `blesensors-mirror` directories, so a successful BLE
copy cannot mark the broader development mirror healthy. A later full standby
deployment may move the same timer names back to the standard shared mirror
service without introducing duplicate schedules. Standby setup defaults off in
the focused playbook so existing continuity arrangements are not assumed.

The BLE product stage excludes ingestion cursors, lock files, and all old/new
staging or previous-store directories. It holds a shared lock on the production
`blesensors.lock` for the complete rsync sender process; the appender's exclusive
lock prevents a source-directory publication swap during that transfer. Lock
waiting is bounded to thirty seconds. A vanished-file exit is a failed BLE
transfer, not a successful freshness update. Other stages retain their existing
behavior. The destination still uses the established per-file delayed rsync
updates; this is source consistency, not whole-store atomic publication on the
development host.

## Optional direct-field continuity

This is a separate, explicit display-continuity option for development when the
authoritative cloud host is unavailable. It does not promote development, enable
normal source/append writers, dispatch archives, or alter other continuity units.
The entrypoint is `playbooks/blesensors_continuity.yml`, limited to
`aurora-cloud-droplet` and guarded against authoritative/non-development hosts.

Its dedicated `aurora-blesensors-continuity.service` sequentially pulls source
files from ASS and runs the reviewed appender every two minutes. It writes only
the new `/data/aurora/dev-products/blesensors-continuity/` data tree: `raw/`,
`state/`, and `blesensors.zarr`. The generated transport script contains no
archive-dispatch hook. Received files are copied additively, including legacy
logs; existing normal raw/product and continuity trees are untouched.

After building a store successfully, activation backs up the existing dashboard
environment and changes only the three BLE keys. The displayed source label is
`SwitchBot via ASS - direct-field continuity`. It does not restart the dashboard
or mobile API; apply their reviewed code/runtime release separately to load the
new bindings and verify visible freshness. The BLE timer is then enabled.

```bash
uv run --offline ansible-playbook playbooks/blesensors_continuity.yml --syntax-check
uv run --offline ansible-playbook playbooks/blesensors_continuity.yml --limit aurora-cloud-droplet --check --diff
uv run --offline ansible-playbook playbooks/blesensors_continuity.yml --limit aurora-cloud-droplet
```

Rollback stops only the dedicated BLE continuity service/timer and restores the
three previous BLE bindings. It leaves copied raw and derived data intact and
preserves any later changes to other environment keys:

```bash
uv run --offline ansible-playbook playbooks/blesensors_continuity.yml --limit aurora-cloud-droplet -e blesensors_continuity_action=rollback --check --diff
uv run --offline ansible-playbook playbooks/blesensors_continuity.yml --limit aurora-cloud-droplet -e blesensors_continuity_action=rollback
```

The baseline and restricted backup directories are retained under
`/var/lib/aurora-cloud/blesensors-continuity/`. Rollback refuses to overwrite a
BLE binding that another release changed after activation. Reload only the
affected reviewed dashboard/API services after rollback to consume the restored
environment. Independent archive completion still requires the authoritative
stream; continuity is not evidence of GWS/object-store delivery.

## Acceptance and rollback

Verify source sync and append successes, then observe two distinct fresh APS
records advancing from ASS to cloud raw to Zarr and the dashboard API. Check
temperature, humidity, battery, sample time, and independent sensor freshness;
missing data must stay missing. Repeat a sync/append and confirm no duplicate
product timestamps. Check the development product when that mirror is enabled.

Raw and product subdirectories are included by the existing full-root GWS and
object-store archive jobs. The BLE archive catalogue and GWS verifier registration
make source completeness visible. Verify a closed raw file against ASS, cloud,
GWS, and object storage. The object-store dispatcher requires a fifteen-minute
settle period: current open files are live data, not yet independently verified
closed archive objects. Actual UTC rotation is therefore required at the field
collector; the legacy logger's process-start filename alone is insufficient.

The managed product archive configuration contains explicit exclusions for the
BLE `.staging.tmp/` and `.previous.tmp/` trees, ingestion cursors, and lock file.
The general `**/*.tmp` rule alone does not exclude temporary directory trees.
The focused archive updater adds these BLE-only exclusions to the deployed GWS
and object-store product wrappers and the product verification policy. Raw jobs
are unchanged. It validates all target formats and requires product archive
workers to be idle before and after changing their configuration; activation
fails closed if a target is unrecognised or a worker is active or queued. Apply
this registration before enabling the authoritative BLE writer. These are local
release changes until the focused playbook has succeeded on that host.

Direct-field continuity is independent of that registration: its isolated tree
is outside the normal product archive source root and has no archive-dispatch
hook. Full-root product archive workers do not take the BLE writer lock, so
their snapshots must not be claimed as transactionally consistent merely
because temporary directories are excluded.

Rollback disables only `aurora-blesensors-source-sync.timer` and
`aurora-blesensors-append.timer` (and the two BLE mirror timers if enabled),
restores the backed-up BLE environment/units and archive bindings, and restores
the prior reviewed application release. Preserve all received raw files and
product data. Restoring archive backups must first confirm no later unrelated
archive configuration changes would be overwritten.

Validation:

```bash
uv run python -m unittest tests.test_blesensors_integration tests.test_power_source_sync tests.test_archive_health_snapshot tests.test_archive_health_presentation
git diff --check
```
