# Preserve older radar days on a second volume

I want enough headroom for the live radar and its native archive without removing
observations. This procedure copies explicitly selected closed days to `/data`,
verifies every file, and keeps the same paths under `/project/aurora/raw/rpgfmcw94`
with persistent read-only bind mounts. The original days stay in quarantine until
the mounted copies and consumer dependencies have been checked.

This is an operator procedure, not an automatic retention policy. Preparing the
helper does not copy data, change mounts, restart a service or release an original.
Keep native backfill paused until the final seven-day capacity check passes.

## The 24 October plan

The reviewed candidates are `Y2026/M05/D02` through `D05`: 96 regular files per day,
384 files in total and 369,295,809,536 allocated bytes (343.94 GiB). At inspection
they had no symlinks, hard links, submounts or open file descriptors. Their newest
modification times were 5–6 May; metadata last changed in July. Those observations
are a reason to consider them closed, not a substitute for the copy-time checks.

The destination is `/data/aurora/raw-relocated/rpgfmcw94/<day>`. The temporary
original is `/project/aurora/recovery/radar-relocation-20261024/<day>`, outside the
raw archive tree. Archive relative paths and file modification times stay the same.
The raw GWS rsync, object-store copy and archive dispatch follow these directory
binds; none uses a one-filesystem restriction. They retain full native archive
coverage. The DO mirror has a separate, selected scope: its `raw-rpgfmcw94` stage
excludes `Y2026/**` and `.radar-partials/**`, while the dashboard receives compact
radar products and quicklooks. Keep those exclusions. This relocation must not
start a new 343.94 GiB historical radar copy to DO.

The dated plan is `inventory/radar-relocation-20261024.example.yml`. Re-measure both
volumes after partial-file preservation has finished and before copying. The
recorded radar arrival rate was 84,856,499,251 bytes/day; seven days plus 8 GiB and
four largest in-flight files require 617,088,629,797 free bytes on `/project` after
release. With these four days, at least 247,792,820,261 bytes must already be free.
The separate `/data` reserve is 40 GiB after the whole copy, covering seven days of
the worst recently observed growth and an operating reserve. Check current growth,
quota and available filesystem space; update the plan if those measurements grow.
Free space expected from a future cleanup does not count as available space.

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

Repeat the stage command for D03, D04 and D05. It copies bytes, owner, group, mode,
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
holds a shared lock through each raw radar sender. A missing nested mount then
fails the transfer without advancing a mirror success stamp. Persist the setting
in the host inventory when commissioned.

## Cut over with the originals still present

Record timer and service states before verification. Once full verification is
complete, pause only the listed radar/raw consumers and their timers; let any
current raw transfer drain. Coordinate the DO `raw-rpgfmcw94` stage with its sender lock. Do not stop the product archive jobs during another operator's
BLE or product commissioning. The helper refuses a running or queued local
consumer. Keep retention paused throughout.

The consumer list covers the live and bulk radar syncs, appender, quicklooks,
archive dispatch, both raw archive copies, mirror verification and retention. Every listed unit receives a
separate drop-in with `Requires`, `After`, `BindsTo` and a failing `ExecStartPre`
mount check. Existing service commands and environment files are unchanged.

Run the full verification while readers and live collection are still running.
It holds a shared helper lock, so cutover cannot race it.

```sh
sudo /usr/local/lib/aurora-radar-relocation.py verify \
  --config /etc/aurora/radar-relocation-plan.json
```

Then pause/drain the listed consumers and perform the short cutover:

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

Before releasing anything, compare every logical path with its destination, run
`systemd-analyze verify` on the generated mount and consumer units, inspect their
`Requires`/`After`/`BindsTo` properties, and test missing-mount rejection with a
separate disposable test plan. Test the dependency stop behavior using disposable
mount/service units, never by unmounting a production volume. No reboot is needed.
Check a normal read through the original logical path and both raw archive tools'
read-only discovery. Preserve that evidence alongside the per-file manifests.

## Release only verified duplicate originals

Run `verify` again with the active configuration while readers are running; the
logical copies are read-only. Then pause/drain consumers for the short release.
`release` checks active mounts, read-only options, both reserves, the dependency
graph and unchanged file identities against that full verification. It writes
durable per-file release intent and completion records before and after unlinking
only the duplicate files in the named quarantine day. It never removes the
logical raw observation or the relocated copy.

```sh
sudo /usr/local/lib/aurora-radar-relocation.py verify
# Pause and drain the recorded consumers after verification completes.
sudo /usr/local/lib/aurora-radar-relocation.py release --day Y2026/M05/D02
```

Repeat for D03–D05 only after the checks above pass. If a release is interrupted,
keep the remaining quarantine files. Re-run `verify`, then resume the same
`release --day` command. A missing original is accepted only if its durable
release intent or completion record exists; an unexpected missing file still
fails. Rollback refuses after the first release intent. Do not use recursive
deletion to finish a release.

After release, verify actual free space on both volumes and two complete raw
archive reports. Restore the timers that were previously running. Keep retention
and native backfill held until their own archive and seven-day headroom gates pass.
The live LV1 timer remains independent of complete native delivery.

## Rollback

Before any original is released, pause/drain the same consumers and run:

```sh
sudo /usr/local/lib/aurora-radar-relocation.py rollback
```

This also handles an interrupted cutover. It verifies the originals, removes only
its own unchanged mount/drop-in definitions, stops the binds, and renames the
original directories back. The `/data` copies remain preserved. Restore the
recorded timer states separately. After a duplicate original has been released,
rollback requires a verified copy back and enough `/project` headroom; the helper
refuses to pretend the old copy still exists.

See [radar source sync](RADAR_SOURCE_SYNC.md), [archive services](ARCHIVE_SERVICES.md)
and the [dated recovery record](records/2026-10-24-primary-restoration.md).
