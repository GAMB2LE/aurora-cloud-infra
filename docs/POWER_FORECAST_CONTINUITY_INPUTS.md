# Power continuity inputs, 23 October 2026

The cached forecast failed at 07:39 UTC because its canonical SOC input was
888.7 minutes old. Fresh continuity Power and PDU data existed, but the runner
and publisher still selected canonical paths. Changing the reader environment
could not change those producer inputs.

This dated repair changed input selection and provenance only. Canonical mode
remained the default. Forecast, battery, load and learning methods were unchanged.
Use [Development experiments](DEVELOPMENT_EXPERIMENTS.md) and
[Releases](RELEASES.md) for current contracts. The
[full original procedure](https://github.com/GAMB2LE/aurora-cloud-infra/blob/7f25708/docs/POWER_FORECAST_CONTINUITY_INPUTS.md)
retains the exact baseline hashes and installation steps; do not replay them
against a later host without checking its installed source.

## Snapshot rules

The producer lock covers copying, consumers and publication. Deterministic and
ENS share a frozen Power/PDU/MQTT snapshot and SOC anchor. A full cycle selects
fresh snapshots again for planning and publication; cached re-anchors select
forecast and publication inputs; standalone ENS selects only forecast inputs.
Snapshots are retired only after their synchronous consumers finish.

Admission limits Power/PDU to 512 MiB and MQTT to 64 MiB. It requires 10 GiB
plus the copy allocation before copying and 10 GiB afterward. All three existing
root-backed cache binds and the systemd storage guard must remain. These checks
are not a quota or a whole-forecast disk monitor.

Actual finite SOC, PDU and parsed MQTT samples must be no more than 20 minutes
old and cannot be future-dated. Dock values are copied unchanged. Missing or
invalid inputs stop the dependent stage and leave the prior complete bundle.
Source trees, canonical mirrors and UAS tiers stay unchanged.

`power_input_selection.json` keeps each phase's selected generation, sample
and SOC times, values and selection time. Retired paths remain provenance only.
The publisher still checks deterministic/ENS anchors and ECMWF identity. A fresh
scenario snapshot cannot relabel an old forecast issue. Cached re-anchors remain
non-independent and ineligible for forecast verification.

## Release and acceptance boundary

Installation required exact runner/publisher baseline hashes, protected backups,
quiescent forecast units and the existing storage guard. Only the three named
forecast services received the new continuity-input environment. No ASFS, met,
field or source/backfill worker was added.

Acceptance needed a normal successful generator/ENS/publisher cycle, a new
complete active bundle, matching real SOC anchor and public forecast checks.
Fresh telemetry alone could not pass. Input-only tests used the producer lock
and bounded snapshots without running forecasts or instrument workers.

Rollback restores the saved runner/publisher and only the added input drop-ins,
then restores recorded forecast timer states. Retain bundles, selection records
and logs; preserve canonical data and continuity producers.
