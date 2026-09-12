# Automatic archive verification recovery

Recovery is **under validation** until `recovery/acceptance.json` reports
`complete`. A successful enqueue, service exit, or one clean audit is not
completion. Production acceptance requires a continuously observed clean window
of at least 48 hours and two subsequent completed 03:20 UTC daily audits.

## Runtime and safety contract

The five-minute, boot-triggered coordinator owns a durable SQLite queue on `/data`.
Daily audits, routine refreshes and post-repair confirmations all enter that queue.
One lane is reserved for raw, one for the oldest non-raw family. Each worker has
at most two concurrent S3 listing requests. Copy writers are unchanged.

| Property | Raw | Each other configured family |
| --- | --- | --- |
| Refresh due from observation start | 3 hours | 12 hours |
| Maximum observation window | 4 hours | 12 hours |
| Evidence expiry | 8 hours | 36 hours |
| Required independent clean confirmations | 2 | 2 |

Independent GWS retention evidence still expires after eight hours. Products
stability requires all constituent families; publishing manifests cannot advance
the heavy-products count. Migrated legacy evidence contributes at most one
confirmation per family. Two post-repair observations are separated by ten minutes.

Each validated S3 page and its continuation cursor commit together. Retries keep
the frozen source snapshot and earlier pages, unless the observation expires or
its source/configuration changes. An invalid continuation token resets only its
shard. Malformed or failed responses never count as empty inventories. SDK retries
are disabled; the queue owns 15/30/60-minute jittered backoff, capped at one hour.
Authentication/configuration failures block explicitly. Transient retries continue
through extended outages without occupying a lane while waiting.

Recovery storage includes queue databases, journals and checkpoints, initially
capped at 10 GiB with a 50 GiB filesystem reserve. Exhaustion pauses collection;
canonical evidence is preserved. Publication produces immutable report, artifacts
and gate generations and switches `latest` atomically under a short commit lock.
Readers and retention batches pin one generation. Remote collection never owns
that commit lock. Repair invalidates the affected audit before copying and
automatically queues two fresh observations afterward.

## Rollout

1. Run the [archive test matrix](ARCHIVE_TESTING.md), Ansible validation and
   dashboard API tests in isolated worktrees. Record both exact source revisions.
2. At an idle verifier/repair/retention boundary, run
   `scripts/backup_archive_recovery.py --destination
   /var/lib/aurora-cloud/recovery-rollbacks/<unique-release-name>` as root. It
   verifies every archived file against the source and records the bundle SHA256.
   Preserve the restricted bundle; it includes the prior canonical `latest` tree.
3. Apply `playbooks/archive_recovery_stage.yml`. This installs compatible readers,
   the hash-locked Python runtime and disabled recovery settings; it preserves
   deployed catalogue mappings and all copy writers and their schedules.
4. Run the credential-isolated pagination probe, then shadow collection using a
   separate `/data` manifest and recovery root. Compare pagination, metadata,
   exclusions and settled comparisons. Never point a shadow catalogue at canonical
   evidence. Record real process-restart checkpoint-resume evidence.
5. Apply `playbooks/archive_recovery_activate.yml` with the release identity,
   verified rollback bundle and shadow evidence. Cutover must occur at an idle
   boundary, with competing triggers fenced. Migrate the legacy `latest` directory
   atomically before enabling queued repair or workers. Preserve `legacy-latest-*`.
6. Verify deployed hashes, service/timer state, both worker lanes, canonical
   report/gate binding, independent GWS state and the public operations response.
   Observe unattended acceptance; do not declare the change complete beforehand.

## Operations

Read actual progress with `sudo aurora-object-store-recovery status`. The
coordinator writes `status.json`; individual workers update their own durable
phase, page and object counts. `daily_audits` completes only after every configured
family supplies a newly started complete observation for that audit.

Valid clean evidence plus a transient retry is explanatory Ops status, not an
active alert. Expired evidence, confirmed settled gaps, permanent failures, a
15-minute coordinator heartbeat gap or six hours of continuing family failure
raise alerts. A delayed listing is reported as verification overdue, not proof
that archive copies are missing. Escalation does not cancel transient retries.
Fresh independent settled source-to-cloud/GWS path discrepancies also raise an
alert, distinct from an S3 copy discrepancy. Missing, malformed or expired path
evidence is verification overdue, never a clean zero or proof of missing files.
These presentation checks do not alter the separate retention-age counters or
claim pruning is paused when independent raw retention evidence remains valid.

HATPRO's `ignore_flat_legacy` rule excludes archive-root duplicates only when
the same exact flat path is no longer required by the current source observation.
The instrument can publish at the root before moving files into dated folders;
both cloud and GWS inventories must retain those active source paths. Verification
never substitutes a same-basename file from another directory, filters the source
snapshot, or extends the existing settling/retention intervals to hide a gap.
Deploy this reader-only correction with `playbooks/mirror_verifier_active_paths.yml`
and a unique `mirror_verifier_release`. The patch preserves the exact deployed
literal configuration, rejects changes outside its three audited reader functions,
requires an idle independent verifier, verifies a restricted rollback copy and
atomically replaces only that reader. It records a corrective intervention and
therefore restarts acceptance on the next coordinator tick. No worker, copier,
source file, service/timer configuration or canonical generation is changed.

Ops evidence age and expiry use the oldest required trusted confirmation from
the pinned family gate, not the newest check or an in-progress worker timestamp.
Refreshing another family cannot extend that published evidence deadline.

If a health read cannot acquire the publication lock within its bounded retries,
it defers to the next normal health timer without overwriting the previous record
or advancing its timestamp. Lock contention is not missing archive evidence.
Prolonged contention therefore still makes health stale; genuinely missing,
unreadable, corrupt or expired evidence remains fail-closed. This presentation
deferral does not change retention checks or any confirmation counts.
Deploy reader-only fixes with `playbooks/archive_recovery_health.yml` and a unique
`archive_recovery_health_release`. Verify the check-mode diff against the live
reader first. The playbook verifies a restricted rollback copy, atomically
replaces only the reader, and records the corrective intervention so the next
normal coordinator tick resets acceptance. Leave `archive_recovery_health_refresh`
false to use the normal health timer; no worker or source writer needs restarting.

After correcting an explicit authentication/configuration block, use
`sudo aurora-object-store-recovery retry --job <exact-family>`. This records a
manual intervention and starts a fresh observation; it does not certify parity.
Do not hand-edit queue records, timestamps, confirmation counts or gate state.
Do not run a second coordinator/worker against the production catalogue manually.

After correcting a local deployment/permission defect, `retry --job <exact-family>
--resume-checkpoint` may resume a blocked observation without discarding validated
pages. It refuses active workers, expired observations and changed configuration;
normal source/checkpoint checks still run. This is recorded as manual corrective
intervention and resets unattended acceptance credit. Never use it to interrupt
healthy automatic retries or to bypass an unresolved block. The default `retry`
still requests an entirely fresh observation.

`aurora-object-store-recovery-upload.timer` retries publishing pinned manifest
generations to the existing object-store manifest destination. Checkpoint storage
and credential files are never uploaded.

## Acceptance and rollback

The coordinator samples acceptance every five minutes. Missing samples/heartbeat,
stale evidence, nonzero independent GWS retention counters, settled discrepancies,
storage violations, manual recovery interventions or incorrect public status reset
the clean observation window. Elapsed time and a green dashboard alone never pass.
Acceptance requires all four independent settled source-to-cloud/GWS missing and
mismatch counters to be present as integer zero, in addition to the separate
retention counters. Those settled counters preserve the verifier's existing
in-flight grace periods; unfiltered fresh raw-report discrepancies do not reset
acceptance. This stricter completion test does not change retention eligibility.
An acceptance-policy upgrade discards earlier clean-window credit once; the next
clean sample starts a new window without changing collector or checkpoint identity.
For an acceptance-only policy correction, use
`playbooks/archive_recovery_acceptance.yml` with a unique
`archive_recovery_acceptance_release`. It verifies a restricted rollback copy
before atomically replacing only the acceptance module. Leave the collector
deployment identity and catalogue unchanged; the next scheduled coordinator tick
loads the correction without restarting workers. Verify the installed hash and
the next acceptance sample before recording deployment success.
The focused playbook records a corrective intervention only when module bytes
change; old unattended credit is not retained across a corrective deployment.
Acceptance reads retry publication-lock contention ten times at 0.2-second
intervals, then defer without writing a successful sample, timestamp or credit.
Known source/coordinator faults and gaps exceeding fifteen minutes still reject
acceptance. Missing, corrupt, unreadable or mismatched evidence is not contention
and still fails closed. Rejections retain a bounded `last_failure` reason across
later healthy samples and emit a sanitized journal event; they never restore
discarded credit. A deferral cannot manufacture the first acceptance record.
A task heartbeat should deliver the final evidence report automatically after the
runtime acceptance record passes; remain quiet while state is unchanged.

Source modification times must be finite and strictly positive. A zero timestamp
can mark an interrupted copy at its final path; it is not proof that a new file
has satisfied the settling interval. Initial, resumed and final source validation
must reject such metadata without silently omitting the file or replacing its
mtime with ctime. Exact-path repair must also refuse it before copying. The
durable `source_metadata` fault remains visible during automatic retries and
disqualifies acceptance immediately, even while older canonical evidence is fresh.
Only a successful fresh verification clears it. Inspect source delivery before
changing anything; matching a partial cloud file to S3 does not establish parity
with the upstream instrument file. This guard does not change copy writers or
authorize repair of source-ingest behavior.

Deploy this guard with `playbooks/archive_source_timestamp_guard.yml` and a unique
`archive_source_timestamp_guard_release`. Both installed inventory entry points,
the recovery module, exact repair program, acceptance reader and health reader
are in scope; catalogue, deployment identity, canonical evidence and checkpoints
are preserved. The playbook requires an idle boundary and verifies restricted
rollback copies before installing code. Busy work is a deployment deferral, not
permission to restart workers or alter scheduling. Acceptance policy 3 starts a
new qualifying window rather than inheriting credit from the weaker policy.

If rollback is required, stop new scheduling and fence verifier, repair and
retention triggers without killing active copy writers. Wait for both workers and
any repair/retention operation to become idle. Verify the restricted bundle SHA256,
restore only its recorded program/configuration/unit paths, and atomically exchange
the retained legacy tree with `latest` using `rollback_legacy`. If that retained
tree is unavailable, restore the bundled canonical tree into a separate directory
and validate it before switching. Restore the recorded prior timer/mask state and
reload systemd. Keep recovery data and the prior generation for investigation;
never delete source archives or bypass expiry/gate checks to obtain a green state.
