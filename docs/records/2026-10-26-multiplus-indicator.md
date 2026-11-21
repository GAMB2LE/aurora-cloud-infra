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

## Web and Android follow-up

The follow-up adds MultiPlus Charger immediately after Power Data on both web
Overview layouts. A browser-only clock updates the displayed sample age and
changes stale readings to Unknown after 30 minutes, including after the page
returns to the foreground. It makes no requests and does not refresh other
products. The initial web value uses the same cached-snapshot helper as the API.

Initial web source: `8c392c54d10be09eced7af5d655ad1948bcad646`, merged through dashboard
PR #33. CI run `40399722501` passed 757 tests and 55 subtests, plus the strict
documentation build. The merged revision passed 117 focused tests and two
subtests. Both local web layouts rendered correctly; a held browser page changed
from Charging to Unknown after the 30-minute cutoff without refreshing.

Development browser acceptance then rejected this candidate: the ESM loader
could not fetch the component's blob under the deployed content security
policy, leaving the page blank. Production was not promoted. A replacement
uses the existing ReactiveHTML runtime and preserves the security policy;
dashboard PR #34 carries that correction. The merged replacement source is
`71eef853ad4d5ca3617e8f856a269e393f187a0d`. CI run `40402550183` passed 758 tests
and 55 subtests plus the strict documentation build. All 118 focused tests and
two subtests passed again on the merged revision, including actual script
execution, lifecycle cleanup and HTML serialization.

Both corrected web layouts rendered with the right styling and card position
behind a local proxy applying the exact deployed content security policy.
Stopping only the local test server left both pages disconnected; each changed
Charging to Unknown at the next local tick beyond 30 minutes. This verifies
offline ageing without interrupting either public server or any field device.

Android 0.14.1 (version code 6), source
`f1a207d6dfa324e6d4f1890f69c5680b2323bb6b`, tag `android-v0.14.1`, adds the matching
charger icon and locally ages cached readings. All 17 device tests passed. Its
signed build and distribution evidence is tracked in the Android repository;
a built APK or AAB does not establish Google Play availability.

The corrected source is tagged `prod-20261026.2`. The focused development
release completed with 23 tasks successful, three changed and no failures.
Its public manifest reports schema 3, revision
`71eef853ad4d5ca3617e8f856a269e393f187a0d` and live-mirror provenance. The browser
rendered the Development live-mirrored banner and the new card immediately
after Power Data: Charging, 1.64 kW, sample 21:19:29 UTC, 10 minutes old.

The primary dry run changed only the three presentation configuration files;
the focused release completed with 15 tasks successful, three changed and no
failures. Both desktop and mobile public Overview layouts rendered the styled
card immediately after Power Data, with no browser warnings or errors. The
primary manifest reports schema 3, the exact replacement revision and
authoritative provenance. It showed Charging, 1.72 kW, from the
21:23:57.389414 UTC sample, observed six to seven minutes old.

Only the two presentation reader pins changed. The primary operations
collector and isolated operational mirror remain on
`c900dd85e71d1a4f81331f92740cbc5049074bf3`. Their existing snapshot contract already
supplies the card. The isolated mirror still reports schema 3 and Charging,
1.64 kW. The primary collector's normal 21:27 UTC run exited successfully;
its source and existing timer were unchanged. `runtime-20261024`, field
settings, observation paths and nginx were preserved. Scientific writer HEADs
matched their pre-release values: primary
`84527ce46626e98961f8484d80dc42fa22fe9e2c`, development
`433cdfa9ab643f043f68ac7c76313946077d2d21`.

Rollback of this follow-up restores both presentation reader pins to
`c900dd85e71d1a4f81331f92740cbc5049074bf3` through the focused presentation role.
Leave the collector and operational mirror pins unchanged.
