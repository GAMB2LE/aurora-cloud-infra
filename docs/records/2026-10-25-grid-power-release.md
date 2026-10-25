# Grid telemetry release, 25 October 2026

The development and JASMIN dashboard/mobile readers now serve dashboard
`prod-20261025.1`, commit `d8942d09681808eb8aad49111badc3ef5a2b302d`, merged through
[dashboard PR30](https://github.com/GAMB2LE/aurora_cloud_dashboard/pull/30).
The existing isolated Python runtime, canonical observation routes and schedules
are unchanged. Both focused presentation dry runs and deployments passed; public
manifests report the expected revision. Power current-condition plots and the
24-hour/96-hour forecast views render. CI passed 725 tests and 55 subtests plus
the documentation build.

The APS logger uses `5bc8387` and is acquiring normally with no automatic
restarts. Its 25 power-schema tests passed. Read-only verification returned grid
AC power, paired MultiPlus DC power and BatteryTemp. The latter is displayed as
**Charger Temperature**, preserving the source meaning in the dashboard docs.
Logger PR9 remains open because main already has an unrelated ASFS collection
failure (`asfssync.WANTED_PREFIXES`); no ASFS code was changed.

The JASMIN shared writer checkout remains on its earlier scientific baseline,
with only additive power-storage and observed-summary support applied as
`power-storage-20261025.2` (`84527ce`). It has not been moved to the presentation
revision. Seventeen storage/summary tests and three subtests passed; scheduled
append runs succeeded and summary products were published at 06:24 UTC. The
unrelated development writer edits were preserved under
`/var/lib/aurora-release-snapshots/grid-power-20261025-before/` and left unchanged.

Daily CSV headers remain fixed. The new optional fields `GridWatts`,
`MultiPlusDCWatts` and `ChargerTemperature` first enter the next daily file,
`power_data_20261026.csv`. Existing raw files and historical missing values are
not rewritten. Corrected forecast writers are **not activated yet**. The user
approved a one-shot follow-up at 00:30 UTC on 26 October to verify raw-to-Zarr
values, run isolated forecast acceptance, and activate only the required power
jobs if those gates pass. The default forecast remains no-grid; the optional
continued-charging curve requires qualified recent DC measurements.

Rollback references are recorded in
`output/grid-power-release-20261025/RELEASE.md` in the GAMB2LE workspace. The prior
presentation pin is `1a58851`; APS and JASMIN writer checkouts have local
`grid-power-before-20261025` tags. Preserve all raw and Zarr data on rollback.
