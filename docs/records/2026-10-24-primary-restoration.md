# Primary restoration: source consolidation, 24 October 2026

This records source consolidation and the bounded live checks listed below.
It does not establish full recovery, mirror acceptance or app beta availability.

## Integrated source

Infrastructure integration began at remote `main` commit `fa25eba`. The original
working checkout remained on `codex/mobile-soc-release-pins-20261019` at
`b61ce03`; its two new commits were reconciled with the four newer `main`
commits in an isolated worktree.

| Completed work | Integrated source |
| --- | --- |
| Current mobile SOC release pins and BLE ingestion | `b61ce03` |
| Deployed HATPRO seed encoding repair and provenance | `2d46b66` |
| Fresh continuity forecast-input snapshots | `633b773` |
| Relocated canonical Power cache guard | `4abc92e` |
| Scoped dashboard code-release tooling | `6819590`, retaining newer inventory pins |
| Additive reconciliation and isolated operational API | `43f29ef` |

The integration checkpoint `151e784` passed 129 focused forecast, BLE and
canonical Power guard tests. The next checkpoint passed 49 focused mirror,
operational API, BLE, cache-guard and nginx checks. These used local synthetic
fixtures and command stubs, without changing live services. The final integrated
local suite passed 544 tests and 247 subtests, with one privileged Linux check
skipped on macOS. All playbooks passed syntax checks, inventory validation
passed, and the documentation built in strict mode. The privileged check still
runs in Linux CI.

## Recovery defaults

The production retention timer is held off in inventory. Mirror deletion is
off, reconciliation is on, and raw/state deletion is rejected even if requested.
The operational fallback API is disabled until separately commissioned with a
reviewed presentation revision and fresh observation evidence.

## Preserved work and remaining evidence

Older ASFS and P50 release-pin branches remain recoverable. Their pins are
superseded by the preserved October pins. The HATPRO top-level binding fix is
already present in the integrated helper and regression tests. They do not overwrite newer
pins. Experimental model work stays isolated; putting tested machinery on
`main` is not scientific promotion. The original checkouts and recovery branches
are retained.

The implementation record must still supply presentation and writer revisions on both hosts, source
and mirror timestamps, storage/quota measurements, archive gates, public docs
publication and native beta status. Until recorded, those outcomes are unverified.

## Merged source and published documentation

PR [59](https://github.com/GAMB2LE/aurora-cloud-infra/pull/59) merged at
06:42 UTC as `0b8cd62ad712327594bd980a3b956f4f60bdb620` after all four required
checks passed. PR [60](https://github.com/GAMB2LE/aurora-cloud-infra/pull/60)
merged at 06:46 UTC as `e536030f7084a03b1048c191a26837ded62a8e8d`, also after
all four checks. The Cloudflare portal published the new
[operational mirror guide](https://gamb2le.pages.dev/aurora-cloud-infrastructure/OPERATIONAL_MOBILE_MIRROR/);
its page content was checked after deployment.

## Radar guard and feed checks, 06:48–06:51 UTC

The focused backfill guard playbook passed check mode, then installed only its
helper and wrapper on JASMIN at 06:48 UTC. The installed wrapper SHA-256 is
`21f46604e8ada494bdf319a554e2141b270d3c8d83b0e3b0fcc289ac79dfda8c`;
the previous wrapper remains in its timestamped backup. Bash syntax and the
mounted capacity check passed. Both radar transfer timers stayed paused and
both worker PIDs stayed zero. This was installation, not backfill acceptance.

Read-only checks showed recovery progressing after earlier quota failures:

| Feed | Verified observation |
| --- | --- |
| Ceilometer | Product advanced from 23 October 15:05 to 18:00; the next scheduled append was processing files through 20:10. |
| HATPRO | The 06:49 append completed and advanced the product to 24 October 05:44:55. |
| WXcam | Catalogue inserted 47 then 84 files; FISH images reached 23 October 18:25 while serial source transfer still worked through the older backlog. |
| Radar | Product remained at 23 October 14:59. Source held a complete newer hour, but the live discovery command expanded its wildcard in the remote home directory and returned empty success. |
| BLE | Source/append units, environment bindings and raw/product directories were absent on JASMIN. Presentation code alone cannot commission its writer. |

The radar discovery defect was reproduced without copying data: the existing
command returned zero bytes for 05:30–06:20 UTC, while the correctly quoted
command found four files. The source wildcard had become 18 shell arguments
instead of the intended four. A separate focused fix adds literal quoting,
bounded capacity admission and a finite recovery-window mode. Its live
commissioning evidence belongs after successful transfer and product checks.

## Bounded radar recovery, 06:55–06:58 UTC

PR [62](https://github.com/GAMB2LE/aurora-cloud-infra/pull/62) passed all four
checks after integrating the current main branch, then merged as
`61b968ebc9a89310ff7156517a8d31993c7b7fed`. The focused deployment changed only
the live wrapper and retained its timestamped original. The deployed SHA-256 was
`b10b81bf7a5254cf87d1632aca71b50fa04a0b130091f6bc013af758eb1cb03f`.

The finite 05:30–06:20 source-modification window selected four files totalling
3,683,680,202 bytes. Its required capacity was 12,273,614,794 bytes, with
152,139,988,992 bytes available. The LV1 arrived first and matched source
SHA-256 `aca1226fcd029a141f14f9224f0c0b385cc54cd67d0feee388e4be0e091e4984`.
The existing pinned radar appender completed at 06:56:28 and advanced the Zarr
to `2026-10-24T05:59:51.806Z`; the public overview then reported Collecting.
The scheduled quicklook service also completed at 06:57:14.

The same finite transfer finished all four native files and enqueued them for
raw archive delivery. The normal checkpoint remained `1792823765`, and both
transfer timers were still paused during these checks. An attempted guarded
stop of the slow remainder found the process already finished and signalled
nothing. This run left no interrupted partial to reclaim. The log remains at
`/var/log/aurora-radar-live-recovery-20261024.log`.

The next focused change deliberately limits the live lane to LV1 NetCDF. The
bounded backfill lane retains every native class, with its backlog and archive
proof tracked independently. Restoring display freshness does not clear that
backlog or satisfy the seven-day capacity gate.

## Live timer restored; native backlog held, 07:00–07:02 UTC

PR [65](https://github.com/GAMB2LE/aurora-cloud-infra/pull/65) passed its four
integrated checks and merged as `e4b8f3061af0e832bba9fd78d893229b05146b8c`.
Only the live wrapper changed on JASMIN. Its SHA-256 is
`76d9de9325d594c62494bc5d7afa28dcf44f842d326571eb777547fb67e843f1`.
A second finite recovery check selected only the 45,138,786-byte LV1 file,
reserved 8,635,073,378 bytes, retained its source checksum and left the live
checkpoint unchanged. The public operations endpoint independently reported
the same `05:59:51.806Z` sample and green collection status. The live timer was
then restored at 07:01 UTC; bulk backfill stayed inactive.

The read-only source inventory measured 84,856,499,251 native bytes modified
in the preceding 24 hours, versus 1,038,510,942 LV1 NetCDF bytes. Seven days at
that native rate plus the four-worker reserve requires 617,088,629,797 bytes;
the measured `/project` availability was 177,985,290,240 bytes. This conservative
growth check assumes no pruning and fails the headroom gate. Production
inventory now holds the backfill timer off so a later broad deployment cannot
silently restore it.

Within the same 24-hour source window, 52 files totalling 47,929,831,411 bytes
were missing or different on cloud, including 13 older LV1 NetCDF files. These
are a visible delivery backlog, not observations to discard. Bounded recovery
of the older LV1 files can proceed within its own reservation. The normal
appender only accepts times after its current frontier, so receiving old raw
files alone does not prove their gaps have been filled in the published Zarr.

### Bounded LV1 raw recovery and the remaining product gap

Three finite recovery windows restored 13 missing older LV1 NetCDF files without
advancing the normal live checkpoint. A source-to-cloud SHA-256 comparison passed
for all 15 available LV1 files in the checked interval, including the previously
recovered hour and the next automatically delivered hour. The latter was selected
by the normal timer at 07:10:07, completed at 07:10:20 and appended by the existing
scheduled writer at 07:14:12. The Zarr frontier then reached
`2026-10-24T06:59:52.536`. Its source/cloud file SHA-256 was
`a5d923f8432a56907f07f835d879d0d44946b78e145db1497469dc5a7bd2388f`.

Older raw files are preserved, but that does not fill the existing Zarr gap. The
current appender deliberately accepts only samples newer than its latest stored
time. The inspected product gap runs from `2026-10-23T14:59:51.910` to
`2026-10-24T04:59:59.854`. No product rebuild was performed. A separate repair would
need an isolated candidate containing the existing store plus available missing
samples, exact checks that existing observations and geometry are unchanged, and
a controlled atomic publication with rollback. The current full rebuild option
moves the published store away before rebuilding a much longer geometry run, so
it is not an appropriate recovery shortcut.

The closed-day [raw relocation procedure](../RADAR_RAW_RELOCATION.md) is prepared
as a separate capacity change. Its example plan and tests do not establish that
any historical day has moved. Copy, mount commissioning, duplicate release and
native-backfill restart each require their own recorded evidence.

The normal radar cycle was also verified through the public operations API after
the monitor refreshed: `collectionSampleAt=2026-10-24T06:59:52.536000Z`, green
collection state, source-sync health 1 and both expected services healthy. This
matched the scheduled Zarr append rather than a quicklook generation timestamp.

The DO operational mirror remains a selected mirror, not a second full native
radar archive. Its `raw-rpgfmcw94` stage explicitly excludes `Y2026/**` and
`.radar-partials/**`; compact radar products and quicklooks supply presentation.
JASMIN and the GWS/object archives retain responsibility for the full native
record; their delivery backlog remains visible. Closed-day relocation preserves
those exclusions and does not add a 343.94 GiB DO transfer.

The inactive relocation helper was prepared from infrastructure main
`d8d8dbae0e84a604f236421a1310f641f35d4462`. Dry-run and apply each reported the four
expected configuration/helper/tmpfiles changes. Its installed SHA-256 matched
`e431d0bebf578d0eaa4047574c3291b7b1d9b2e01e2736dfb1527a7f41bee63f`.
The unprivileged remote reader could run the inactive guard. The live LV1 timer
remained active, bulk backfill remained inactive, and no relocation mount was
activated. At the preparation check, `/project` had 274,043,240,448 bytes free and
`/data` had 490,631,454,720; the helper calculated 369,295,809,536 bytes of potential
reclaim from the four closed days. These are dated measurements, not a release
receipt or permission to skip a later capacity check.

### Closed-day copy started, 07:23 UTC

After the partial-file preservation completed, a fresh check measured
278,619,226,112 free bytes on `/project` and 490,587,414,528 on `/data`. Both staging
gates passed. The durable oneshot
`aurora-radar-relocation-stage-20261024.service` started at 07:23:04 and copies
D02–D05 sequentially with the reviewed helper. Progress and completed-day results
are recorded in `/var/log/aurora-radar-relocation-stage-20261024.log`; a day counts
as staged only after its manifest is published. An active service or a partly
copied destination is not a completed SHA verification.

The original logical paths remain unchanged. No active relocation record,
production bind mount, quarantine rename or duplicate release has been performed.
Live collection continues, and native backfill and retention remain held. The
[archive follow-up](2026-10-24-archive-follow-up.md) records the separate metadata,
native backlog and stale verification findings.

### Disposable mount failure checks, 07:32–07:36 UTC

The isolated pre-cutover test found that systemd could create a missing bind
source as an empty directory. The earlier guard accepted the mount identity alone.
No production relocation had been activated. The helper now asserts that the
destination already exists and checks its exact staged file set and metadata
against the SHA manifest before allowing a consumer to start. The graph check
also handles systemd's quoted, escaped mount names.

At 07:36 the candidate passed the full disposable sequence: a missing mount was
rejected; the valid read-only mount and consumer started; stopping the bind stopped
the consumer; an absent bind source failed without creating an empty directory;
a missing file and a changed file each failed the consumer's pre-start check; and
restoring the verified test fixture allowed normal startup again. Unit validation
passed. The test used separate directories outside raw and runtime-only units;
the production active record stayed absent, both production volumes stayed
mounted, and all test units and mounts were removed afterward.

The receipt remains at
`/project/aurora/recovery/radar-relocation-probe-20261024f/probe-result.json`.
The candidate SHA-256 was
`4d5063247d21651bc56f9ab6dba831e5d93b61d35b43ddfbaa5aebbe2a8a43a9`.
These checks validate the failure gates; they do not establish that the four
production days have finished staging or authorize their cutover.

### Consumer guards before cutover, 07:39–07:44 UTC

The reviewed relocation helper was installed from main `ce04c326` at 07:39:09,
with a backup of the previous helper. The staging process continued unchanged.
The primary transfer guard was then installed through the narrow release
playbook. Its SHA-256 is
`f26cd65d35f89f884b43cb650e26ec10ee0c0a9885aebaf687da7f1cec8ecaef`.
The check passed with 278,589,865,984 available bytes against the live lane's
8 GiB reserve. The source wrapper was unchanged, the live timer remained active,
and native backfill remained inactive.

The mirror inventory now retains the same sender guard on future releases.
The deployment preview changes only the transport wrapper: it wraps the radar
sender in the primary's shared relocation lock. The actual mirror SSH route
successfully ran the helper as `azimuth`, read the disposable manifest, and
returned the unchanged command while the production active record was absent.
The canonical Power storage guard also passed. This records a successful
preflight; mirror wrapper installation and its next scheduled stage still need
their own receipt. The selected mirror scope and native exclusions are unchanged.

Both guard hooks remain inactive until the production relocation record exists.
Creating that record still requires completed manifests for all four days,
fresh capacity checks and review of the cutover evidence.

### Mirror guard installed, 07:46 UTC

Infrastructure PR80 merged as main `458ea0a`. The final preview again changed
only the mirror transport wrapper and passed its checksum and canonical Power
storage checks. Installation completed at 07:46:15 with a preserved backup at
`/usr/local/bin/aurora-dev-live-pull.2946643.2026-10-24@07:46:15~`.
The installed SHA-256 is
`0640f4897ecfdbc72cd58f16fcb861de03836650db8efe73cfca5476b36eb958`.
The existing scheduled `raw-rpgfmcw94` stage then succeeded at 07:46:21 in one
second, with zero bytes needing transfer. No timer, producer or mount was
reconfigured. The production active relocation record remained absent.

D02 completed staging at 07:44:20: all 96 files and 93,041,524,863 logical bytes
were verified. Its manifest at
`/var/lib/aurora-cloud/radar-relocation-20261024/Y2026-M05-D02.json` has SHA-256
`019ecca979f5acda2a60e9f95e93550504e92bee38a3c824c2551d5f941d8338`.
D03 started next. This first receipt does not stand in for the remaining three
days; cutover and release remain held until all four receipts and the final
capacity checks have been reviewed.
