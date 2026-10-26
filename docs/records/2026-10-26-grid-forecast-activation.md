# Grid power forecast activation preparation — 26 October 2026

This record describes the prepared release, not a completed deployment.
Forecast activation remains gated on fresh qualified telemetry, isolated output
validation, and successful required CI and release checks.

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
active writer to release this change.

Activation requires raw CSV through canonical Zarr and display-product checks,
followed by isolated no-grid and continued-charging forecast generation using
fresh measurements and existing weather inputs. Confirm SOC anchors, DC coverage,
bounds, missing/stale-data behavior and archive metadata before any live forecast
run. After release, verify public forecast/API output and the added power and
temperature traces. If a gate fails, retain the existing forecast selection and
report the specific blocker. Preserve raw data and unrelated checkout changes.
