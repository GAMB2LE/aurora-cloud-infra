# Archive recovery

I start with `sudo aurora-object-store-recovery status`. The coordinator owns
the queue, retries and daily audits. I do not launch another worker against the
production catalogue to make an in-progress audit finish sooner.

## How it works

A boot-triggered, five-minute coordinator keeps durable state on `/data`.
One lane is reserved for raw; the other serves the oldest due non-raw family.
Copy writers continue independently.

| Rule | Raw | Other families |
| --- | --- | --- |
| Refresh due from observation start | 3 hours | 12 hours |
| Maximum observation window | 4 hours | 12 hours |
| Evidence expires after | 8 hours | 36 hours |
| Independent clean confirmations | 2 | 2 |

A validated S3 page and its cursor commit together. A transient failure keeps
that work and uses the queue's 15/30/60-minute backoff, capped at an hour.
An invalid continuation token resets only its shard. Expired observations or
changed source/configuration require new evidence. Failed or malformed pages
never become empty inventories. Authentication and configuration faults block
explicitly until corrected.

Workers publish immutable report and gate generations. Readers pin one
consistent generation. Each family keeps its own original proof time;
refreshing products cannot renew raw evidence. Recovery storage is bounded
and preserves the last canonical evidence when capacity runs out.

## What needs attention

Normal settling, current verification and checkpointed transient retries are
status, not proof of an outage. [Archive alerts](ARCHIVE_SERVICES.md#what-the-alert-means)
separate pending delivery from overdue paths and verification faults.

Investigate a permanently blocked family, invalid source metadata, expired
proof, a 15-minute coordinator heartbeat gap or six hours of continuing family
failure. Inspect the current queue, worker identity, heartbeat and counters
before restarting anything. Missing or zero source mtimes must be repaired at
the source stage; they cannot be replaced with ctime or dropped from the audit.

After correcting an explicit block, retry the exact family:

```bash
sudo aurora-object-store-recovery retry --job <family>
```

`--resume-checkpoint` may retain validated pages after a local deployment or
permission repair. It still rejects active workers, expiry and changed
configuration. Either manual retry records an intervention and resets
unattended acceptance credit. Healthy automatic retries need no manual replay.

## Focused code changes

Use the matching playbook rather than the whole site deployment:

| Change | Playbook |
| --- | --- |
| Initial compatible readers and disabled recovery settings | `archive_recovery_stage.yml` |
| Reviewed activation with rollback and shadow evidence | `archive_recovery_activate.yml` |
| Independent verifier logic | `mirror_verifier_active_paths.yml` |
| Health presentation | `archive_recovery_health.yml` |
| Acceptance policy only | `archive_recovery_acceptance.yml` |
| Invalid-source timestamp guard | `archive_source_timestamp_guard.yml` |

Check the live hashes, preserve restricted rollback copies and wait for the
required idle boundary. Keep catalogue bindings, checkpoints and timer state
unless they are explicitly part of the reviewed change. A storage-guarded
entrypoint needs its versioned pinned payload updated while preserving the
wrapper; the plain reader patch refuses that wrapper.

Test in an isolated catalogue first. Check pagination, source/GWS/S3 coverage
and a real process restart from a saved cursor. Shadow evidence must have its
own storage root. Never use production objects or retention records as fault
injection fixtures.

## Historical radar helpers

`roles/radar_archive_recovery/files/` contains the versioned radar recovery
helpers. They require the matching host release and durable state; this is not
an automatically installed Ansible role.

A historical coordinator may be inactive after successful completion. The
fresh controller accepts that only through `completed_coordinator_gate.py`,
which checks the exact release, completed queue, frozen versions and pair
ledger. Do not restart a finished coordinator just to satisfy a liveness check.
Unresolved attempts still need reconciliation; completion is not permission
to reuse an old worker or discard a conflicting file.

## Acceptance is a separate result

Read `recovery/acceptance.json`. Recovery is under validation until that record
reports `complete`: at least 48 continuously observed clean hours and two
subsequent completed 03:20 UTC daily audits are required.

Acceptance rechecks configured family coverage, distinct confirmations,
source/cloud/GWS counters, retention proof, storage bounds, coordinator
heartbeat and the public API. Manual interventions or observation gaps reset
the window. Quiet pending-path presentation does not waive these stricter
checks. A green dashboard, one clean audit or elapsed time alone is not acceptance.

If publication is briefly busy, readers defer without replacing evidence or
advancing timestamps. Missing, corrupt or expired evidence remains a failure.
See [Archive testing](ARCHIVE_TESTING.md) for the test matrix.

## Rollback

Pause new scheduling and let active work finish. Verify the recorded rollback
bundle, restore only its listed code/configuration/unit paths, then restore
the prior timer and mask state. Keep both evidence generations and all
recovery state. Do not roll an old cursor over newer progress, remove source
archives or relax a gate to force a green result.
