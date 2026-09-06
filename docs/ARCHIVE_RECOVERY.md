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

Ops evidence age and expiry use the oldest required trusted confirmation from
the pinned family gate, not the newest check or an in-progress worker timestamp.
Refreshing another family cannot extend that published evidence deadline.

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
A task heartbeat should deliver the final evidence report automatically after the
runtime acceptance record passes; remain quiet while state is unchanged.

If rollback is required, stop new scheduling and fence verifier, repair and
retention triggers without killing active copy writers. Wait for both workers and
any repair/retention operation to become idle. Verify the restricted bundle SHA256,
restore only its recorded program/configuration/unit paths, and atomically exchange
the retained legacy tree with `latest` using `rollback_legacy`. If that retained
tree is unavailable, restore the bundled canonical tree into a separate directory
and validate it before switching. Restore the recorded prior timer/mask state and
reload systemd. Keep recovery data and the prior generation for investigation;
never delete source archives or bypass expiry/gate checks to obtain a green state.
