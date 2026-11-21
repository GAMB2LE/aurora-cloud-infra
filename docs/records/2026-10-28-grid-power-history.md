# Grid Power history presentation release — 28 October 2026

Status: primary and development browser/API acceptance passed; the isolated
operational API passed its manifest and Current Power contract checks.

Current Power gives Grid Power its own historical chart immediately after
Renewables on web and native clients. Grid AC input and MultiPlus DC charging
have separate labels and retain their signed recorded values, timestamps and
gaps. They move out of the Battery Charging chart. The current reading card and
Forecast sections retain their existing behavior.

The existing raw, current-display and combined-display power products already
contain both optional fields on primary and development. No collector, scientific
writer, forecast or field acquisition change is needed. The desktop reader
adapts older Current prewarm figures in memory to the new layout; published
prewarm JSON remains unchanged. Native apps use the existing generic panel
contract, so this chart does not require a new app binary.

## Candidate checks

Dashboard [PR #36](https://github.com/GAMB2LE/aurora_cloud_dashboard/pull/36)
contains tested candidate `c6c30f5e12aa8d98c7390763cf21c05c8d24d404`. Local UTC
validation passed 835 tests and 55 subtests with four optional skips, plus
strict documentation and credential checks. Actual primary/development prewarm
figures were used to test the reader transformation and reviewed visually.
Android fixture UI validation passed. iOS source compatibility with the existing
generic panel contract was checked; an iOS visual smoke test was unavailable.
The chart uses the existing native response contract without requiring a new
app binary.

CI run `40782466886` passed all 839 tests and 55 subtests in 9 minutes 59 seconds,
plus the strict documentation and public-module import checks. PR #36 merged
normally at 22:59:07 UTC as `8f40db64e880452304550c2bb8b3a1c5dc883f6c`.
Its source tree `276fa64e8f200fd42a96f944dd10de0b93fd3525` is identical to the
tested candidate. No full test rerun was needed for this identical tree.

## Development acceptance

The focused check diff changed only presentation provenance and the two reader
overrides. Apply completed with 23 tasks successful, three changed and no
failures. The public manifest reports schema 3, exact revision
`8f40db64e880452304550c2bb8b3a1c5dc883f6c`, live-mirror provenance and the ordinary
development dashboard URL.

Current API panel order begins Renewables, Grid Power, Battery Charging. The new
panel has Grid AC input and dashed MultiPlus DC charging on one watts axis;
Battery Charging no longer repeats them. Both traces had 160 bounded points,
ending at 22:50 UTC with 1840.0625 W AC input and 1683.47791 W DC charging.
Development receipts taken before the separate mirror promotion confirmed
unchanged collector, forecast and isolated mirror service configuration,
collector timer and scientific writer HEAD.

Fresh development Current Power navigation rendered Grid Power immediately below
Renewables. Browser inspection confirmed the watts axis, solid purple Grid AC
input, dashed green MultiPlus DC charging, readable legend and existing observed
plots. The Development banner was present. Only after this acceptance was the
annotated `prod-20261028.1` tag pushed at the exact merged revision.

## Primary and isolated mirror acceptance

The primary focused diff changed only presentation provenance and the two reader
overrides. Apply completed with 15 tasks successful, three changed and no
failures. The isolated operational API role completed with 11 tasks successful,
two changed and no failures, without running nginx tasks. Its changes were the
reader source/provenance and service source directory; the existing runtime and
isolation settings were retained.

All three public APIs serve the new Grid Power panel in the same position and
with the same AC/DC trace contract. Primary and development manifests report the
full merged revision; the isolated mirror reports its matching 12-character
revision. Primary remains authoritative. Development and the isolated API remain
live mirrors; only the isolated API has `operationalMirror: true` and the JASMIN
dashboard URL. The isolated API traces also had 160 points ending at 22:50 UTC,
matching the accepted primary and development samples.

Fresh primary Current Power browser navigation confirmed Grid Power immediately
below Renewables, one Power [W] axis, solid purple Grid AC input, dashed green
MultiPlus DC charging and no error. Final service receipts at 23:06 UTC on
primary and 23:09 UTC on development confirm active readers, clean exact source,
unchanged scientific writer HEADs, unchanged collector timer text and unchanged
collector/forecast commands and service configuration. The intended presentation
provenance environment changed; collector code and acquisition behavior did not.

## Preserved baseline

Before release, both ordinary presentation readers and the isolated operational
API served `7399c08a60e37fa2b7b48b21369b1bfb6ac54c1d`. The primary operations
collector also used that source; its source pin and command are outside this
presentation release. The existing runtime is
`/opt/aurora-presentations/runtime-20261024`.

Read-only receipts captured on 28 October at 22:39 UTC confirmed clean deployed
presentation source and unchanged scientific writer HEADs: primary
`84527ce46626e98961f8484d80dc42fa22fe9e2c`, development
`433cdfa9ab643f043f68ac7c76313946077d2d21`. Forecast services use their existing
shared writer checkout/runtime. Neither host requires a Tailscale change for
this release.

The local release packet holds both actual Current prewarm JSON files, the
three public API manifests and Current Power responses before and after release,
effective service/source receipts and SHA-256 manifests. These are dated release
evidence, not proof of a later deployment.

## Rollout and rollback

The focused presentation role updates only ordinary web/API reader provenance
and commands, with development browser acceptance before production promotion.
The isolated operational API uses its existing role without the nginx task.
Inventory changes are limited to the two presentation revisions and the isolated
mirror revision/app directory. Collector, writer and forecast pins, runtimes,
settings, schedules, published products, data paths, nginx and equipment remain
unchanged.

Rollback restores the affected readers to
`7399c08a60e37fa2b7b48b21369b1bfb6ac54c1d` with the same focused roles and existing
runtime. It does not restore, rewrite or remove observations or prewarm products.
