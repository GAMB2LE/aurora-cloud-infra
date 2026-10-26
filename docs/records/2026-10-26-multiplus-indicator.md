# MultiPlus charging indicator release — 26 October 2026

The native Overview API now places MultiPlus Charger directly below Power Data.
It uses the latest recorded MultiPlusDCWatts sample in the existing cloud power
product. The existing operations collector publishes the value and source time;
API refreshes use that cached snapshot and never query field equipment.

Dashboard source: `c900dd85e71d1a4f81331f92740cbc5049074bf3`, release tag
`prod-20261026.1`. The primary and development presentation readers, the primary
operations collector and the isolated operational mirror use that exact source
in separate checkouts with the existing `runtime-20261024` environment.

The 748 backend regression tests and 55 subtests passed in CI. All 131 focused
checks and 21 subtests passed again on the merged revision. Staging and production
rendered their dashboards; the staged helper read charging power from the stored
replica without invoking the full collector or making field requests.

The primary collector's normal 20:32 UTC run completed successfully. Its charger
sample was 20:29:14.704253 UTC, 1610.885986328125 W, exactly matching the stored
power product at that timestamp. The public primary API showed Charging, 1.61 kW,
and the sample age. Missing/invalid/future readings and readings older than
30 minutes show Unknown; zero or negative output means Not charging, not Off.

The release preserved field acquisition, charger settings, scientific writer
checkouts and runtimes, timers, data paths, network configuration and nginx.
The operational API was updated using only its existing role, without applying
the nginx route task. No extra collection run was started.

The iOS release is tracked separately in the app repository; server deployment
alone does not establish that a new TestFlight build is available.

Rollback restores the preceding presentation pin
`d8942d09681808eb8aad49111badc3ef5a2b302d` and preceding primary-collector and
operational-mirror pin `1a588513a6f715f2159d0f65c41b857f1ccca9d3`, using the
same focused roles. Preserve all observation data and existing schedules.
