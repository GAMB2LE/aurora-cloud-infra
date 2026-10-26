# Grid Power card release — 26 October 2026

Server release accepted at 22:21 UTC. Native app distribution is tracked
separately below.

The Current Conditions section of Power gains a Grid Power card above its charts
on desktop and mobile web. Native apps receive the same card through the optional
`currentSignals` field on Current, Observed and All Power responses. Forecast
responses are unchanged. App build and store availability remain separate from
the server release.

The card reports signed `GridWatts`, the recorded AC input measurement. Positive
input is green; zero and negative readings remain neutral and do not claim that
the grid is disconnected. It requires its own valid, nonfuture measurement time.
Missing or invalid values, and samples older than 30 minutes, show Unknown. The
browser clock updates age and expires cached readings locally, including while
disconnected and after returning to the foreground.

The existing cloud operations collector reads the final `GridWatts` and
`MultiPlusDCWatts` row together from its existing local power product. It neither
fills gaps from older finite readings nor queries field equipment. Failure of
either optional field leaves a valid sibling measurement intact. API and browser
card reads use the cached operations JSON; no polling cadence or device control
changes are part of this release.

## Candidate and verification

Dashboard [PR #35](https://github.com/GAMB2LE/aurora_cloud_dashboard/pull/35)
merged as `7399c08a60e37fa2b7b48b21369b1bfb6ac54c1d`, with an identical source tree
to candidate `7f1fc05ecc2956fef19164b155f64e1d8851892d`. Development acceptance
passed before the annotated production tag `prod-20261026.3` was pushed at that
exact revision. CI run
`40409496192` passed 812 tests and 55 subtests, public-module import checks and
the strict documentation build.

Local checks passed 114 web/display/shell/session tests and two subtests. The
actual ReactiveHTML scripts were exercised for positive, zero and negative grid
power, charger behavior, expiry without network access, foreground events, and
repeated-render/removal cleanup. After isolating test view state, all 20 grid and
browser-clock tests passed again. Collector/API regression coverage verifies the
single cached-product open, final-row gaps, own timestamps, invalid values and
optional-field failure isolation. All 231 focused checks and two subtests passed
again on the merged revision.

Local browser acceptance of the candidate passed through a proxy using the exact
deployed content security policy. Both desktop and mobile Current Power rendered
the styled Grid Power card above the charts, showing 1.93 kW and its own sample
age. Desktop Forecast hid the card; returning to Current restored it. After
stopping only the local Panel test server, both disconnected pages changed to
Unknown with Last sample 30m ago at the next local clock tick. The test servers
and tabs were then removed. Neither public host nor field equipment was
interrupted for this check.

## Deployment acceptance

The focused development release completed with 23 tasks successful, three
changed and no failures. Its actual browser rendered the styled card, existing
charts and Development live-mirror banner without browser errors or warnings.
The public manifest reported schema 3, the exact merged revision and live-mirror
provenance. `currentSignals` correctly returned Unknown with a null sample before
the primary collector update reached the snapshot.

The staged helper read only the existing development Zarr, without invoking the
full collector: GridWatts was 1838 W and MultiPlusDCWatts was 1679.58 W at
21:56:03.244806 UTC. The shared card returned 1.84 kW with an age of 16 minutes.
This checked the data path without an extra device request or collection run.

The combined primary presentation/collector release completed with 29 tasks
successful, five changed and no failures. The operational API-only mirror
release completed with 11 tasks successful, two changed and no failures; its
nginx task was not applied. Primary desktop/mobile and development desktop
rendered the fresh Grid Power card above their charts without browser errors.

At 22:21 UTC the primary, development and isolated operational mirror APIs all
reported 1.88 kW from the same sample at 22:15:28.226902 UTC. Their manifests
reported the release revision, using the full SHA on the ordinary endpoints and
its short form on the isolated mirror. The API response evidence is retained
with the local release packet as `primary-api.json`, `development-api.json` and
`operational-mirror-api.json`.

The primary collector's normal 22:17:00–22:17:19 UTC run exited successfully.
Its snapshot, timestamped 22:17:01.115014 UTC, contained 1884 W GridWatts and
1701.290039 W MultiPlusDCWatts, both stamped 22:15:28.226902 UTC and matching the
raw product's final row. Normal development replication succeeded at 22:17:54
UTC and carried those fields to both mirror APIs. No additional collector run
was started.

The release used the focused presentation role on both hosts, the primary-only
operations collector role and the existing operational mirror role. It retained
`/opt/aurora-presentations/runtime-20261024`, environment and output paths, and
the existing `*:01/5` collector timer. Only the reviewed reader/collector source
pins changed. Scientific writer HEADs remained primary
`84527ce46626e98961f8484d80dc42fa22fe9e2c` and development
`433cdfa9ab643f043f68ac7c76313946077d2d21`. Field acquisition/settings, writer
commands/runtimes, other timers, observation paths, network settings and nginx
were unchanged.

## Native app distribution

The corresponding native work is iOS 0.13.3 (build 30),
[PR #16](https://github.com/GAMB2LE/aurora-dashboard-ios/pull/16), and Android
0.14.2 (version code 7),
[PR #8](https://github.com/GAMB2LE/aurora-dashboard-android/pull/8). At server
acceptance, final release artifacts and store uploads remained pending. The
iOS upload required the Mac to be unlocked; Google Play required sign-in to the
existing owner developer account. Server acceptance does not establish that
either new app build is available to testers.

## Rollback

Restore both presentation reader pins to
`71eef853ad4d5ca3617e8f856a269e393f187a0d`. Restore the primary collector pin and
isolated operational mirror source/revision to
`c900dd85e71d1a4f81331f92740cbc5049074bf3`, using their existing focused roles and
the same runtime. Preserve all observations and existing schedules; wait for
the next scheduled collector run rather than starting an extra collection.
