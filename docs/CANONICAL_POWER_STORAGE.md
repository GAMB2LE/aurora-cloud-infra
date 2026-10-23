# Canonical Power mirror storage protection

The DigitalOcean standby keeps the canonical Power path stable while its
`ecmwf_solar_planning` cache uses root-disk capacity through an existing bind:

```
/srv/aurora-relocated/canonical-power/ecmwf_solar_planning
  -> /data/aurora/products/power/ecmwf_solar_planning
```

This role preserves an independently verified relocation. It does not move or
delete data, create a mount, change archive authority, or alter scientific
processing. The original cutover must separately establish the bind, persist it
across reboot, checksum the copy, and release only the verified redundant copy.

## Enabling and deploying

`aurora_standby_canonical_power_guard_enabled` defaults to `false`. Enable it
only for `aurora-cloud-droplet`, after verifying that the cutover is complete.
The optional task file validates the exact Power source/destination paths and
runs the guard's read-only `--check` before installing any persistent guard,
drop-in, or replacement mirror script. This validation also runs in Ansible
check mode. A missing or incorrect mount, symlink endpoint, unmounted `/data`,
wrong backing device, or insufficient reserve fails deployment before the
mirror script changes. The guard does not rely solely on a directory existing.

The only new systemd drop-in is
`aurora-dev-live-pull@product-power.service.d/90-canonical-planning-cache.conf`.
It uses `RequiresMountsFor` and `ExecStartPre`, so a failed check is visible as a
failed start. The role does not create a new timer or restart a service in its
Power guard tasks. A full standby role run retains its existing broader service
management; do not use that as a scoped operational cutover. Apply an approved
deployment separately, accounting for active mirror processes.

## Runtime behavior

The Power branch shares `/run/aurora-canonical-power-mirror.lock` between an
all-stage mirror and the dedicated Power instance. A held lock returns 75;
an all-stage invocation can finish its other branches but cannot advertise
global success. The lock is also available to a separately reviewed relocation.

Power transfers run in two parts: normal Power files explicitly exclude the
planning subtree, followed by the planning cache itself. Both run through the
same guard at 8 MiB/s. Both must succeed before the original `product-power`
success stamp advances; transferred bytes are aggregated. Source locks and
their existing vanished-file policy are preserved. In particular, BLE keeps
its shared remote producer lock, and raw radar keeps its `Y2026/**` exclusion.
All non-Power stage paths, exclusions, and transfer decisions are unchanged.

The guard stops Power transfers below 22 GiB free on root or 10 GiB on `/data`,
providing buffers above the 20 GiB and 8 GiB operational reserves. It checks the
layout and space before starting, every two seconds, and after successful or
vanished-source (rsync 24) exits. Its private process group is terminated on a
hold or interruption. Transfers have a one-hour deadline. These are monitored
thresholds, not hard quotas against unrelated concurrent writers.

## Verification and rollback

Read `/proc/mounts` or `findmnt` and run
`/usr/local/lib/aurora-canonical-power-guard.py --check` before accepting a
deployment. Confirm the effective Power drop-in, successful scheduled mirror
exit, both component stamps and the final aggregated Power stamp. A held or
failed transfer must leave the preceding success stamp intact. Check current
public products separately: mirror success alone does not prove freshness.

Rollback needs a coordinated storage plan. Do not disable the flag or remove
the guard while leaving a relocated cache exposed to an unguarded mirror.
Do not unmount the bind or delete either copy while writers are active. The
role intentionally does not automate a relocation rollback or clean up an
existing drop-in when the feature flag is false.

Focused local checks:

```
python -m pytest tests/test_canonical_power_guard.py tests/test_blesensors_mirror.py -q
```

These test the rendered mirror in isolated temporary directories, including
lock holds, precheck refusal, second-transfer failure, truthful success stamps,
byte aggregation, non-Power branch parity, the raw-radar exclusion, and BLE
source-lock behavior. Guard checks cover invalid mounts, reserves before and
after transfers, and child cleanup. No live services are exercised.
