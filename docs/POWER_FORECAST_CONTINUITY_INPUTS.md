# Power forecast continuity inputs — 23 October 2026

## Diagnosis

The07:39 scheduled cached re-anchor passed the repaired cache-mount/free-space guard, then failed because the hardcoded canonical Power store's latest SOC was888.7minutes old. The canonical store ends22October16:50:42; the published continuity generation inspected at07:42 had Power through23October07:32:51 and PDU through07:33:01, with31days history. Those two stores total69.3MB. The existing forecasting runner's six Power arguments, two PDU arguments, and publisher Power/PDU/UAS arguments all pointed to canonical mirrored paths. Changing the serving environment alone could not repair those inputs.

## Patch boundary

The exact deployed orchestration runner and publisher are patched only for input selection and provenance. No forecast, battery, load, dock-tier, or learning algorithms change. Canonical input mode remains the default. Continuity is enabled only by the separate environment file supplied here; it requires explicit continuity root, private snapshot root, and UAS log.

The existing producer lock covers input copying, all forecast consumers and publication. Each invocation resolves one published continuity generation and copies only Power/PDU stores plus the separately published private MQTT log. The snapshot persists until every subprocess finishes; EXIT cleanup removes only its uniquely marked directory. A later invocation cleans marked abandoned snapshots while holding the same producer lock. This avoids long-running forecasts losing Zarr chunks when continuity retires an older generation. Source directories, canonical mirrors, and UAS tiers are never modified.

Admission caps the combined snapshot at512MiB, the MQTT log at64MiB, and checks tenGiB available space plus the allocation before copying and tenGiB again afterwards. The existing systemd storage guard must remain installed, including all three root-backed cache binds. These are admission checks, not a filesystem quota or a forecast-wide disk monitor. Actual latest finite BatterySOC, PDU measurement and parsed MQTT timestamps must be within20minutes, with no future timestamps. Both docks' log values are copied unchanged. Any missing/invalid input or copy failure stops before scientific generation or publication. The old complete forecast bundle stays available.

The runner passes the same selected Power/PDU pair to deterministic, ENS and planning generation. The publisher passes that same pair and selected UAS log to operating scenarios/display generation, and preserves `power_input_selection.json` in the resulting bundle. Canonical paths/default behavior remain unchanged when the explicit mode is absent. The existing ASFS input in the power display builder is unchanged; this patch runs no ASFS/CRD/met source, repair, or backfill worker.

## Exact deployed baselines

- `/usr/local/bin/aurora-power-forecast-runner`: SHA256 `5a8db417f0928d9916372e704a5f1bfa139a378845f92caaf29c202d5b7d1e7f` (`runner.before.sh`).
- `/usr/local/bin/aurora-power-forecast-publish`: SHA256 `810b40756e0e30019215cbf37be211980a919e80536dad9ec944ab1a82189bd3` (`publisher.before.py`).

Stop the two scheduled forecast timers during installation, confirm forecast/re-anchor/ensemble/publisher units have no running process, and preserve backups of the two installed scripts and affected drop-ins. Check these baseline hashes before replacement; abort on a mismatch. No immutable application release is edited.

## Install

1. Install the supplied `aurora-power-forecast-inputs.py` at `/usr/local/lib/aurora-power-forecast-inputs.py`, root:root0755.
2. Atomically install the supplied `aurora-power-forecast-runner` and `aurora-power-forecast-publish` over their corresponding `/usr/local/bin/` paths, root:root0755. Run shell syntax/Python compilation first.
3. Create `/data/aurora/dev-products/power/forecast-inputs`, aurora:aurora0750. Install `aurora-power-continuity-inputs.env` at `/etc/aurora-power-continuity-inputs.env`, root:aurora0640. Its UAS path is the already verified private overlay `/data/aurora/dev-products/uas-continuity/menapia_mqtt.log`.
4. Install `95-continuity-inputs.conf` into the `.service.d` directories for `aurora-power-soc-forecast`, `aurora-power-soc-forecast-learn`, and `aurora-power-soc-ensemble`. Do not add this environment to standalone publisher or unrelated services. The runner supplies its selected snapshot environment to its publisher subprocess. Preserve existing90-storage-guard.conf and other limits/drop-ins.
5. Reload systemd. Verify all three effective units retain the storage ExecCondition/cache mounts and load the new environment. Restore the previously enabled forecast and learn timers. The standalone ensemble timer remains as previously configured.

## Validation

- Test results and the independent review are recorded beside this runbook. Local tests use synthetic stores/logs and command stubs; they do not run field/scientific pipelines.
- For a live input-only validation, invoke only the helper under the existing producer flock as the aurora user, using the dashboard venv. It should create a bounded snapshot and selection manifest. Inspect `last-selection.json` and its actual sample times, then run the helper's `cleanup` subcommand before releasing the lock. This does not invoke any forecasting or instrument worker. Alternatively, let the normal scheduled forecast provide this validation.
- Observe a normal cached re-anchor or an explicitly coordinated scoped Power forecast. Verify `last-selection.json` identifies the continuity generation and private UAS source snapshot. Require successful generator/ENS/publisher completion and a new complete `forecast_bundle_status.json`/active generation, not just a successful storage ExecCondition.
- Inspect the new active bundle's `power_input_selection.json` and operational forecast `initial_soc_time`. Its SOC anchor must match a real selected fresh Power sample; deterministic/ENS anchor-matching checks remain unchanged. Cached re-anchors must remain non-independent and verification-ineligible as before.
- The continuity display worker normally rebuilds `current/display/power_forecast_display.zarr` from configured forecast products. Its next unmodified scheduled publication should carry the new real forecast anchor. Verify the public Power forecast API and both serving hosts/apps separately; no manual shared continuity run is required. A recent Power telemetry card alone does not prove forecast recovery.

## Rollback

Stop/hold only forecast and learn timers, wait for their units to finish or coordinate cancellation, remove only the newly added95-continuity-inputs.conf drop-ins, restore the exact backed-up runner/publisher scripts, reload systemd, then restore the original timer states. Retain output bundles, input-selection records and logs for evidence. The helper and unreferenced private snapshot directory can stay installed; do not delete canonical/forecast data or change source/continuity workers to roll back this I/O patch.
