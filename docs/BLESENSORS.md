# BLE enclosure temperature and humidity

APS and ASS SwitchBot readings arrive through the ASS collector. They are
separate from scientific surface meteorology and Operations telemetry. This
integration adds no email or notification rule.

| Layer | Path |
| --- | --- |
| ASS source | `/home/aurora/data/blesensors/` |
| Production raw | `/project/aurora/raw/blesensors/` |
| Product | `/data/aurora/products/blesensors/blesensors.zarr` |

Source and append timers run every two minutes on the authoritative writer.
Sync is additive, imports available history and revisits a ten-day overlap.
CSV wins over legacy logs for the same sensor/UTC second; late CSV and repeated
imports must not duplicate samples. Legacy filename dates do not limit parsing.
The application uses `BLESENSORS_DIR`, `BLESENSORS_ZARR_PATH` and
`BLESENSORS_SOURCE_LABEL` (normally `SwitchBot via ASS`).

## Production release

The [commissioning record](records/2026-10-24-blesensors-commissioning.md)
holds the dated installed revisions and observed cycles. For a new release,
install reviewed dashboard/appender code and its runtime first. Keep dedicated
BLE pins in host inventory when the shared writer checkout lacks that appender:

```yaml
blesensors_app_dir: /opt/aurora-presentations/<full-commit>
blesensors_venv: /opt/aurora-presentations/runtime-<release-date>
```

Both default to the shared application settings. These examples do not install
code or establish runtime readiness. Follow [Releases](RELEASES.md), then run:

```bash
uv run ansible-playbook playbooks/blesensors.yml --limit aurora-cloud --check --diff
uv run ansible-playbook playbooks/blesensors.yml --limit aurora-cloud
```

The focused playbook changes BLE units, directories, three environment keys and
BLE archive bindings. It preserves other writers and installed verifier/health
code. It checks all five archive targets, retains modes/ownership and backs up
under `/var/backups/aurora-blesensors/<release>/`. Unknown formats fail closed.
Product archive workers must be idle and unqueued before and after registration;
register exclusions before enabling the BLE writer. BLE pruning stays disabled.

Tags are `blesensors_source`, `blesensors_processing`, `blesensors_archive` and
`blesensors_standby`. To stage without activation, set
`blesensors_source_sync_timer_enabled=false` and
`aurora_processing_timers={"blesensors_append":false}` for this focused
playbook only. Remove those overrides for activation. Never use that small
processing dictionary with a broad runtime release.

## Development mirror

Development normally receives `raw-blesensors` and `product-blesensors` from
production. To add only those stages on an existing host:

```bash
uv run ansible-playbook playbooks/blesensors.yml --limit aurora-cloud-droplet --check --diff -e blesensors_configure_standby=true
uv run ansible-playbook playbooks/blesensors.yml --limit aurora-cloud-droplet -e blesensors_configure_standby=true
```

The dedicated mirror has separate state/stamps and additive transfers. A BLE
success cannot mark every stream healthy. A later full standby deployment may
reuse the same timer names with the shared mirror; it must not add duplicate
schedules. Focused standby configuration defaults off.

The sender holds the producer's shared `blesensors.lock` for the whole rsync,
with a 30-second wait. It excludes cursors, locks, staging and previous stores.
A vanished file fails the transfer and preserves the old stamp. This protects
the source during copying; delayed per-file updates do not make the destination
Zarr publication atomic.

## Optional direct-field continuity

`playbooks/blesensors_continuity.yml` is restricted to non-authoritative
development. It pulls ASS and appends every two minutes into the isolated
`/data/aurora/dev-products/blesensors-continuity/` tree (`raw/`, `state/`,
`blesensors.zarr`). It does not promote development or dispatch archives.

After a successful build, activation backs up and changes only the three BLE
bindings, using label `SwitchBot via ASS - direct-field continuity`. It enables
its own timer. Release/reload reviewed reader code separately and verify visible
freshness; this playbook does not restart the dashboard or API.

```bash
uv run ansible-playbook playbooks/blesensors_continuity.yml --limit aurora-cloud-droplet --check --diff
uv run ansible-playbook playbooks/blesensors_continuity.yml --limit aurora-cloud-droplet
```

For rollback, use the same commands with
`-e blesensors_continuity_action=rollback`. This stops only its dedicated units
and restores the three saved bindings. It refuses to overwrite a later BLE
release and preserves other settings and all copied data. Backups remain under
`/var/lib/aurora-cloud/blesensors-continuity/`. Reload affected readers separately.

## Acceptance and rollback

Observe two fresh APS records through ASS, cloud raw, Zarr and the API. Check
temperature, humidity, battery and each sensor's actual sample time. Missing
values stay missing. Repeat sync/append and check for duplicate product times;
verify the development product separately when enabled.

Check one closed raw file against ASS, production, GWS and object storage.
Dispatch needs 15 minutes of settling. The field collector must rotate by UTC;
a process-start filename alone does not prove closure. Product archive bindings
exclude `.staging.tmp/`, `.previous.tmp/`, cursors and locks explicitly; a generic
`**/*.tmp` pattern does not cover those directory trees. Full-root archive
writers do not take the BLE producer lock, so these exclusions alone do not
prove a transactionally consistent Zarr snapshot. Continuity is outside the
normal archive root and supplies no archive proof.

To roll back production, disable only the BLE source/append timers and enabled
BLE mirror timers. Restore the saved BLE units, bindings and reviewed application
release after checking that no later unrelated archive changes would be lost.
Preserve all raw files and products. Tests are in
[Archive testing](ARCHIVE_TESTING.md).
