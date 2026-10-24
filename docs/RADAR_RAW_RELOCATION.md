# Preserve older radar days on a second volume

I want enough headroom for the live radar and its native archive without removing
observations. This procedure copies explicitly selected closed days to `/data`,
verifies every file, and keeps the same paths under `/project/aurora/raw/rpgfmcw94`
with persistent read-only bind mounts. The original days stay in quarantine until
the mounted copies and consumer dependencies have been checked.

This is an operator procedure, not an automatic retention policy. Preparing the
helper does not copy data, change mounts, restart a service or release an original.
Preparation installs inert launch conditions and reloads systemd; it does not create
the temporary operator hold.
Keep native backfill paused until the final seven-day capacity check passes.

## The 24 October plan

The first reviewed candidates are `Y2026/M05/D02` through `D05`: 96 regular files per day,
384 files in total and 369,295,809,536 allocated bytes (343.94 GiB). At inspection
they had no symlinks, hard links, submounts or open file descriptors. Their newest
modification times were 5–6 May; metadata last changed in July. Those observations
are a reason to consider them closed, not a substitute for the copy-time checks.

The plan also includes only `Y2026/M05/D13`, with an explicit 64-file expectation:
16 files of each native type, 61,415,642,624 allocated bytes. This was a partial
historical acquisition day; changing its expectation does not weaken the 96-file
checks for D02–D05. Two source metadata inventories were identical at 08:04:39 and
08:05:11; the newest file mtime was 14 May. Both open-file and process-map scans
found no reader, and there were no links or submounts. All five days total
430,711,452,160 allocated bytes and 448 files. Queue D13 only after the current
four-day stage finishes; do not run heavy stage copies in parallel.

The destination is `/data/aurora/raw-relocated/rpgfmcw94/<day>`. The temporary
original is `/project/aurora/recovery/radar-relocation-20261024/<day>`, outside the
raw archive tree. Archive relative paths and file modification times stay the same.
The raw GWS rsync, object-store copy and archive dispatch follow these directory
binds; none uses a one-filesystem restriction. They retain full native archive
coverage. The DO mirror has a separate, selected scope: its `raw-rpgfmcw94` stage
excludes `Y2026/**` and `.radar-partials/**`, while the dashboard receives compact
radar products and quicklooks. Keep those exclusions. This relocation must not
start a historical native radar copy to DO.

The dated plan is `inventory/radar-relocation-20261024.example.yml`. Re-measure both
volumes before copying and cutover. The recorded arrival rate was 84,856,499,251
bytes/day; seven future days plus 8 GiB and four largest in-flight files require
617,088,629,797 free bytes on `/project` after delivery of existing debt. The fresh
08:06 source comparison found 44 missing native files totalling 51,015,441,637
bytes. That first plan required 668,104,071,434 bytes before native backfill.
The four original days alone would not meet this post-backlog headroom gate.

At 08:06, the five-day projection left 709,263,569,408 bytes on `/project`, or
658,248,127,771 after the known backlog: 41,159,497,974 above the future seven-day
target. `/data` projected 58,176,908,800 bytes after all remaining copies, exceeding
its separate 40 GiB reserve by 15,227,235,840 bytes. Already staged allocations were
subtracted once, including partial D03. These are dated estimates, not permission
to spend future free space. Recheck allocation, arrival rate, debt, quota and actual
available bytes before D13 staging, cutover and backfill. Keep backfill held until
its fresh post-backlog capacity calculation passes.

The 08:53 refresh found 54,622,066,409 bytes of native debt, raising the dated
primary minimum to 671,710,696,206 bytes. The active archive recovery has a stricter
50 GiB `/data` floor, so the plan now uses that instead of the earlier 40 GiB.
Preparation rejects a lower reserve when archive recovery is enabled. Re-read its
actual configured reserve and use the stricter requirement before any copy or cutover.

Also reserve measured non-staging growth for the remaining copy, consumer drain
and full mounted-verification window. Track `/data` free bytes plus the actual
allocated stage bytes to distinguish other growth from copying. Sample free space
after counting pending copy bytes; a concurrent completed copy must not be credited
against an older free-space reading. Credit source allocation only while every
selected day remains unmounted on the original `/project` filesystem. After release,
use actual free space without adding the moved bytes again. If the time allowance
would cross the reserve, hold the next stage or cutover and report the shortfall.
Do not weaken the floor or assume future cache cleanup.

`expected_files_by_day` permits a positive exact count only for an explicitly
listed day. The reviewed D13 override leaves all other days at 96. No other older
day is included in this plan.

## Prepare and stage

Use an exact reviewed infrastructure commit reachable from main. Preserve the
installed helper and wrapper checksums in the dated recovery evidence.

```sh
uv run ansible-playbook playbooks/radar_raw_relocation_prepare.yml \
  --limit aurora-cloud -e @inventory/radar-relocation-20261024.example.yml --check --diff
uv run ansible-playbook playbooks/radar_raw_relocation_prepare.yml \
  --limit aurora-cloud -e @inventory/radar-relocation-20261024.example.yml
```

On JASMIN, inspect the complete plan and stage one day at a time:

```sh
sudo /usr/local/lib/aurora-radar-relocation.py inspect \
  --config /etc/aurora/radar-relocation-plan.json
sudo /usr/local/lib/aurora-radar-relocation.py stage \
  --config /etc/aurora/radar-relocation-plan.json --day Y2026/M05/D02
```

Repeat the stage command for D03, D04 and D05, then D13 after refreshing both
volume gates. It copies bytes, owner, group, mode,
modification time and extended attributes, then independently reads SHA-256 from
both files. It checks source identity and file-set stability and fsyncs the copy
and manifest. Existing complete copies are verified before reuse. An interrupted
`.copying-*` file is retained and blocks a retry: inspect and preserve it separately
before restarting that day. Unknown destination files are never overwritten.

Staging leaves the current paths untouched. Normal live collection may continue.
Do not run simultaneous stage commands for the same day.

Before cutover, install the updated transfer guard through the narrow radar guard
playbook while its source timer is paused. Enable
`aurora_standby_raw_relocation_guard: true` for the DO transport, using the exact
installed wrapper checksum with `playbooks/mirror_transport_release.yml`. Prepare
the primary helper first. That remote guard is harmless before activation and
holds a shared lock in its parent process until each raw radar sender exits.
This includes privileged senders: `sudo` closes inherited file descriptors, so
passing the lock into `sudo` is insufficient. The parent forwards termination
signals to the sender group, waits for completion and returns its status without
adding output to the rsync protocol. A missing nested mount then
fails the transfer without advancing a mirror success stamp. Persist the setting
in the host inventory when commissioned.

If a sender supervisor is killed forcibly or crashes, inspect and drain any
remaining remote rsync processes before cutover; an unlocked file alone is not
proof that an orphaned reader has stopped.

## Cut over with the originals still present

Record timer and service states before verification, including the recovery
launcher (`aurora-object-store-recovery.service` and its timer) and DO's raw radar
stage. Preserve pre-existing failed states; do not use `reset-failed`.

The plan lists twelve raw readers: live and bulk radar sync, appender, quicklooks,
archive dispatch, both raw archive copies, mirror verification, retention, the raw
recovery worker, shared repair, and recheck-after-repair. The latter two can start
from worker completion hooks; a single inactive check does not close that race.
Preparation installs a separate negative `ConditionPathExists` drop-in for each
reader. The absent `/etc/aurora/radar-relocation.hold` leaves them unchanged.

After checking the fresh stage proofs, create that persistent operator hold before draining.
Pause the recovery launch timer and wait for its short coordinator service to
finish, so condition-skipped launches do not add noise to the durable queue. Keep
existing product verification workers and recovery-manifest upload running. The
full and incremental inventory units may remain enabled only after their actual
live commands have been checked: they must enqueue requests, not directly walk
raw files. Keep their queue and checkpoint files untouched.

Pause only the recorded raw timers and drain all twelve readers. Coordinate DO's
`raw-rpgfmcw94` timer and inspect remote rsync processes separately from the shared
lock. The helper checks the hold, unchanged condition drop-ins, loaded systemd
configuration, inactive/drained consumers and recovery launcher before changing
paths. All twelve readers also receive mount `Requires`, `After`, `BindsTo` and a
failing `ExecStartPre` guard at cutover. Existing commands and environments stay
unchanged.

Keep retention held throughout. Its independent persistent
`/etc/aurora/ass-retention.hold` remains while either `ass_retention_enabled` or
`ass_retention_timer_enabled` is false, including direct archive-triggered starts.
Use `playbooks/retention_hold.yml` for a narrow deployment; it does not run or reset
retention. The full role removes that hold only when both switches are explicitly
enabled after their separate gates pass.

A completed stage is already a full SHA-256 verification of both copies. When
all five completed manifests are less than six hours old, cutover can use those
proofs: under the exclusive lock it checks unchanged source and destination file
sets and identities before the first rename. There is no need to repeat the same
full read before a reversible path change. The originals remain in quarantine.

If a proof is older, run full verification while readers and collection continue,
then investigate any changed identity rather than bypassing it:

```sh
sudo /usr/local/lib/aurora-radar-relocation.py verify \
  --config /etc/aurora/radar-relocation-plan.json
```

That command holds a shared lock. Cutover cannot race it. A complete additional
five-day read would read about 861.42 GB across both copies; the dated estimate is
60–100 minutes, separate from staging and dependent on storage/cache throughput.
The full read after mounting remains mandatory before releasing any original.

Then create the temporary barrier, pause the recovery launcher and recorded raw
timers, drain readers and perform the short cutover. Record ownership of the hold
in the dated evidence and fsync its file and parent directory before proceeding:

```sh
sudo python3 - <<'PYHOLD'
import os
from pathlib import Path
path = Path('/etc/aurora/radar-relocation.hold')
# Exclusive creation refuses to overwrite somebody else's operation.
with path.open('x') as stream:
    stream.write('Reviewed radar relocation, 2026-10-24\n')
    stream.flush()
    os.fsync(stream.fileno())
fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
os.fsync(fd)
os.close(fd)
PYHOLD
sudo systemctl stop aurora-object-store-recovery.timer
# Wait for the coordinator, current raw readers and remote senders to drain.
```

Then:

```sh
sudo /usr/local/lib/aurora-radar-relocation.py cutover \
  --config /etc/aurora/radar-relocation-plan.json
sudo /usr/local/lib/aurora-radar-relocation.py guard
sudo /usr/local/lib/aurora-radar-relocation.py graph
```

Cutover repeats the reserve checks and compares every source/destination identity,
size, modification time and change time with the completed SHA verification. The
proof must be less than six hours old. It publishes the active guard record,
renames each original into quarantine, and enables a persistent read-only bind
mount. Each mount requires both `project.mount` and `data.mount`. The active guard
record is written before the first rename: an interruption therefore blocks raw
consumers instead of exposing an empty tree as success. Timers remain paused.

Each bind unit asserts that its destination directory already exists; systemd
must not create an empty replacement. The consumer guard also compares the exact
file set and file identities, sizes, times and ownership against the completed
SHA manifest. These are metadata reads, not another full archive hash. A missing,
empty or changed destination fails even when the bind itself is mounted read-only.

Before releasing anything, compare every logical path with its destination, run
`systemd-analyze verify` on the generated mount and consumer units, inspect their
`Requires`/`After`/`BindsTo` properties, and test missing-mount rejection with a
separate disposable test plan. Test the dependency stop behavior using disposable
mount/service units, never by unmounting a production volume. No reboot is needed.
Check a normal read through the original logical path and both raw archive tools'
read-only discovery. Preserve that evidence alongside the per-file manifests.

## Release only verified duplicate originals

Run `verify` with the active configuration while readers are running; the
logical copies are read-only. It checks the mount guard before and after reading,
hashes every logical bound path and its quarantined original, and rejects a
changed target or active cutover record during that read. Only a fully successful
pass publishes a durable mounted verification receipt tied to the exact manifest,
active cutover record and logical/destination directory identities. The active
record is bound by SHA-256 and filesystem identity, so a new cutover requires new
mounted verification even if its plan is identical. An interrupted pass leaves
the previous proof intact.

A staging-only proof cannot authorize release. An equivalent same-target remount
with unchanged files remains valid; the normal guard still requires the correct
read-only bind. Then recreate the same owned launch hold and pause/drain consumers
and the recovery launcher for the short release.
`release` checks active mounts, read-only options, both reserves, the dependency
graph, mounted verification receipt and unchanged file identities against that
full verification. It repeats the receipt/cutover identity check before each
unlink. It writes durable per-file release intent and completion records before
and after unlinking only the duplicate files in the named quarantine day. It never removes the
logical raw observation or the relocated copy.

```sh
sudo /usr/local/lib/aurora-radar-relocation.py verify
# Pause and drain the recorded consumers after verification completes.
sudo /usr/local/lib/aurora-radar-relocation.py release --day Y2026/M05/D02
```

Repeat for D03–D05 and D13 only after the checks above pass. If a release is interrupted,
keep the remaining quarantine files. Re-run `verify`, then resume the same
`release --day` command. A missing original is accepted only if its durable
release intent or completion record exists; an unexpected missing file still
fails. Rollback refuses after the first release intent. Do not use recursive
deletion to finish a release.

After each successful mount/graph/read-through check, remove only the temporary
relocation hold owned by this operation and restore the recorded raw timer states.
Leave the independent retention hold in place. Replay one ordinary recovery tick
and one shared repair start to process durable queued requests and the latest
report. A condition-skipped raw launch remains `launching` for at most 120 seconds;
reconciliation preserves its remaining confirmations, epoch, checkpoint and retry
attempt count. The coordinator pause avoids those skipped launches during cutover.

After release, verify actual free space on both volumes and two complete raw
archive reports. Restore the timers that were previously running. Keep retention
and native backfill held until their own archive and seven-day headroom gates pass.
The live LV1 timer remains independent of complete native delivery.

## Rollback

Before any original is released, activate the same temporary hold, pause/drain
the same consumers and recovery launcher, then run:

```sh
sudo /usr/local/lib/aurora-radar-relocation.py rollback
```

This also handles an interrupted cutover with a missing or failed bind. Rollback
verifies the quarantined originals and destination directly; it does not falsely
claim a successful mounted publication or require a healthy mount to recover.
It removes only
its own unchanged mount/drop-in definitions, stops the binds, and renames the
original directories back. The `/data` copies remain preserved. Restore the
recorded timer states separately. After a duplicate original has been released,
rollback requires a verified copy back and enough `/project` headroom; the helper
refuses to pretend the old copy still exists.

See [radar source sync](RADAR_SOURCE_SYNC.md), [archive services](ARCHIVE_SERVICES.md)
and the [dated recovery record](records/2026-10-24-primary-restoration.md).
