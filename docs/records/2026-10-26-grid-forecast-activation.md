# Grid power forecast activation preparation — 26 October 2026

This release is prepared but activation is blocked. The existing forecast jobs
and their selected code remain unchanged on both hosts.

The canonical JASMIN path passed the raw CSV/Zarr comparison on 26 October:
1,109 matched recent rows contained all three fields through 09:48:54 UTC, with
1,109 qualified station-load rows and a ready continued-charging rate of
1,718.64 W. Public production and development current-data APIs expose Grid AC
input, MultiPlus DC charging and Charger Temperature in their intended plots.

The development forecast has a different input path: its runner selects frozen
continuity observations. Generation `20261026T094305Z-3888450` has none of
`GridWatts`, `MultiPlusDCWatts` or `ChargerTemperature` in its power Zarr, despite
being built from today's raw CSV. The snapshot helper copies that dataset
without filtering columns. New forecast code would therefore lack qualified DC
input. Leave source selection and forecast jobs unchanged until the existing
continuity producer preserves these optional fields and all gates are rerun.

Isolated validation also found a pandas mask dtype failure at an MPPT telemetry
gap. Dashboard PR31 fixes this without changing calibration eligibility; it was
merged after 726 tests and 55 subtests passed. This code is staged, not selected
by live forecast jobs. Infrastructure PR97 remains a draft; its four CI checks
passed before this outcome record was added.

The repaired isolated run completed successfully in 8 minutes 13 seconds using
canonical observations and copied existing weather inputs. Operational and
planning outputs used load model 11; operating output used model 12. Read-only
acceptance confirmed a shared initial SOC, 97 bounded hourly conditional samples
through 96 hours, a qualified DC rate of 1,723.602 W, the measured-DC contract,
stale/missing-input rejection and matching recommendation-archive metadata.
The baseline inferred station load was 1,685.9 W. All generated products, state
and archives are isolated under `/data/aurora/grid-validation-20261026`; no
published forecast was replaced by this validation.

The selected source is `01fd146d4d40313fa1c5b72c34a9a64d6b575f9a`, which adds
the MPPT gap-handling fix from dashboard PR31 to the reviewed Grid Power,
MultiPlus DC charging and Charger Temperature changes. Its intended runtime is
the existing `/opt/aurora-presentations/runtime-20261024` installation. Both hosts
have independent forecast pins; their shared scientific writer checkouts remain
separately managed.

`playbooks/power_forecast_code_release.yml` checks the exact clean, main-reachable
source and selected runtime before changing paths in the existing forecast,
learning, ensemble, planning and operating-scenario units. On the development
host, it also selects those paths in the existing runner, publisher and failure
handler. It preserves the deployed command arguments, outputs, locks, ownership,
sandbox and drop-ins instead of regenerating the units. Each changed file receives
an Ansible backup. The playbook validates the units and reloads systemd; it does
not start jobs or alter timers. The matching template variables preserve these
independent pins during later routine configuration updates.

Before selecting paths, record the active state of the existing forecast timers,
temporarily stop only those that were active, and wait until all forecast writers
are idle. This closes the timer-start race during file replacement. Restore
exactly those previously active timers afterward, including after a failed
attempt; leave their schedules and enablement unchanged. Do not interrupt an
active writer to release this change. Append-service OnSuccess actions can also
start forecast learning, so the release guard must cover these triggers as well
as timers before changing live paths.

Activation requires raw CSV through canonical Zarr and display-product checks,
followed by isolated no-grid and continued-charging forecast generation using
fresh measurements and existing weather inputs. Confirm SOC anchors, DC coverage,
bounds, missing/stale-data behavior and archive metadata before any live forecast
run. After release, verify public forecast/API output and the added power and
temperature traces. If a gate fails, retain the existing forecast selection and
report the specific blocker. Preserve raw data and unrelated checkout changes.
