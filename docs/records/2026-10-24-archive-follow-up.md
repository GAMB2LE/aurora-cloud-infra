# Archive follow-up, 24 October 2026

These are dated checks during the primary restoration. They do not declare the
archive clean or authorize retention. Native radar backfill and retention remain
held while the separate capacity and archive gates are completed.

## Raw source metadata

The old raw verification error named
`asfs/crd/aurora_asfs_data_sci_202610231530.dat`. By 06:37 that file had been
repaired by normal source delivery. A read-only check found valid modification
time and identical SHA-256 on ASS and JASMIN. Its companion fast-sonic file also
matched. The 07:24 raw verification moved past ASFS and found the next blocker;
the aggregate status file retained the earlier error until the next coordinator
tick. Use `aurora-object-store-recovery status` and the individual worker journal
when diagnosing a retry.

A complete scan found exactly three remaining files with invalid modification
times, all empty HATPRO files at the raw archive root:

| Flat filename suffix | Complete dated source and cloud SHA-256 |
| --- | --- |
| `261023_204709.ATN` | `8cd9f0eb81bb825c2fa72add568098a8ae474859acd99750cd3b0430a3dd9013` |
| `261024_024708.ATN` | `07132c06fa37d6a166fd7fa83e4af855009b3ba72aa916e923ad48f424ebe93c` |
| `261024_034708.ATN` | `eba9de2cbe3731cce37e1573207fee61334f75719544b4d904fb23cc2115949f` |

Each filename begins `HATPROG5-AURORA-ICELAND_`. The flat files had size zero and
mtime zero after the quota failure. Each dated counterpart was 1,571,057 bytes;
the corresponding flat path was absent on ASS. No observation timestamp was
invented to satisfy the verifier.

At 07:30:09–11, the three empty placeholders were preserved by same-filesystem
rename to `/project/aurora/recovery/hatpro-empty-placeholders-20261024/`. The
operation held the existing shared HATPRO live/backfill lock, checked that no
process held an open file descriptor, repeated the source absence and dated
source/cloud SHA checks immediately before each move, and fsynced its manifest
and both directories. It retained each inode, owner, mode, zero mtime and extended
attributes. No observations were deleted and no timer was stopped.

The durable manifest is `manifest.json` in that recovery directory, SHA-256
`310d8e71951a998666b6735834acfca8148ac2ee7b3cea14f1a98c08470f9595`.
The existing recovery queue accepted a fresh raw retry as generation 12 at
07:30:47, and its worker started at 07:30:48. That launch is not yet a completed
clean comparison. Manual correction also restarts the separate recovery
acceptance window; it does not waive the required independent confirmations.

## Native radar delivery debt

The refreshed source/cloud comparison found 42 missing native files totalling
50,991,223,097 bytes: 14 each of LV0, LV0 NetCDF and native LV1. All available LV1
NetCDF files in the recovered interval were present. The GWS snapshot also had
three LV1 NetCDF files awaiting its normal raw transfer. Those are dated delivery
counts, not a reason to discard source files or weaken a verifier.

The live LV1 lane and complete native archive have deliberately separate capacity
and freshness checks. The [radar recovery record](2026-10-24-primary-restoration.md)
records source-to-product/API evidence and the remaining historical product gap.

## Object-store reports and the camera race

The last complete comparison reported 27,559 settled missing objects, but its raw
observation started at 14:58 on 23 October and its products observation at 03:23 on
24 October. The entries comprised 36 WX JPEGs totalling 11,720,753 bytes and 27,523
product objects totalling 2,776,220 bytes, mostly small Power Zarr objects. Read-only
S3 probes of one reported WX JPEG and one Power metadata object found both already
present at their expected sizes. The old count cannot establish the current debt;
fresh complete family observations are needed.

The 07:04 raw copy ended at 07:09 with exit 6 after rclone tried to stat a temporary
receiver file that had disappeared:
`auroracam/radar-cam/2026-10-24/.radar-cam_2026-10-24_03-09.jpg.WOG86N`.
It had already copied 3,266 files. This failure is distinct from the empty HATPRO
metadata and from a missing final observation.

The installed rclone 1.53.3 local backend calls `lstat` before file filters, and
records a missing directory entry as a non-retry error. A camera filename
exclusion alone therefore cannot close this race. See the
[upstream implementation](https://github.com/rclone/rclone/blob/v1.53.3/backend/local/local.go#L395-L414).
Do not treat all exit-6 failures as success. A focused receiver change would need
to keep temporary files outside the archived tree on the same filesystem, preserve
partial transfers, and prove interrupted-transfer and atomic-promotion behaviour.
No such receiver change was deployed during these checks.

The existing raw writer already reserves an eight-minute recent slice and a
22-minute historical slice. The exact-report repair copies only named settled
paths and rechecks their source evidence, but its service allows up to 12 hours
and has no independent byte or file-count cap. Do not launch every old reported
product gap as an assumed bounded repair. Correct the metadata blocker, obtain
fresh evidence, and use the existing family queue before considering further work.

## ASFS storage estimate is stale, 07:41–07:46 UTC

The collector's exact read-only SSH route to ASS succeeded. The source file
`/home/aurora/data/asfs/logger_storage_health.json` was generated at
`2026-10-21T13:31:32.424153+00:00`, so it is outside the collector's two-hour
freshness limit. Its old estimate of 65,256 directory slots is not a current card
measurement or evidence of today's capacity. The ASS data filesystem had about
2.61 TB free during this check. The read succeeded; the unavailable estimate
comes from its age and absent producer, not a failed SSH fetch.

The current `/home/aurora/scripts/asfs/asfssync.py` is a 3,240-byte FTP mirror with
mtime `2026-10-21T14:58:42.143040+00:00` and SHA-256
`967a5a9b9b0b05324025bc677cfc66bb606b582d8e0c12dfdafe478f9226814d`.
It has no storage-health writer. The old `asfssync.service` and timer were not
loaded, and their journal had no entries for this morning. Updating the cloud
collector cannot refresh a file its source no longer produces.

Current source observations were still arriving: science file
`aurora_asfs_data_sci_202610240730.dat` contained 25,167 bytes, fast gas
`aurora_asfs_data_fast_gas_202610240730.dat` contained 1,950,930 bytes, and fast
sonic `aurora_asfs_data_fast_sonic_202610240701.dat` contained 331,961 bytes. Their
source mtimes were 07:31:00–07:31:05 on 24 October. These are collection evidence,
not a replacement storage estimate. I left field acquisition and current data
untouched. Restoring the estimate producer is separate follow-up work.


## Fresh raw comparison and persistent retention hold, 07:58–08:03 UTC

The fresh raw observation completed at 07:58:03 and published at 07:58:05. It
compared 893,083 settled local observations with S3: all matched, with zero missing,
size-mismatched or checksum-mismatched entries. S3 contained 1,867 additional
entries, which were retained. This is 100% coverage for that settled local scope.
The separate canonical-source/GWS comparison matched 317,849 of 317,888 entries:
39 native radar entries were missing, with zero size or checksum mismatches.
Its 572,115 additional GWS entries reflect the wider archive scope and were kept.
The unsettled window contained 17 missing and two size-different entries; those
are pending transfer evidence, not settled failures.

The raw worker finished successfully at 07:58:08 and queued its next independent
confirmation. Overall clean/stable parity and raw retention readiness were still
false. The separate products observation was still running; the older 27,523
missing-product count does not describe its unfinished result. The normal raw
copy retry from 07:34:40 completed successfully at 07:57:44 without another camera
temporary-file error.

Both native-backfill and retention timers were disabled while inactive at 07:55;
the live LV1 timer stayed enabled and active. Their failed service states were
preserved. Disabling the retention timer was not sufficient because archive
workers can directly request the service. PR 84, main `a625538`, added an independent
persistent service condition. The focused apply at 08:02:43–44 reported six tasks
ok, three changed and no failures. The hold and condition are root-owned.

Before and after that apply, retention remained `failed`, result `exit-code`, with
MainPID zero and unchanged previous start/exit times of 23 October 14:55:56 and
14:58:20. A read-only systemd condition evaluation rejected activation while held.
A separate disposable service had already proved held, released and held-again
behaviour. No production pruning run was used as a test and no failed-state evidence
was reset. The hold remains until both inventory switches are explicitly enabled
and the independent archive and capacity checks pass.

The four-day relocation's 617,088,629,797-byte reserve covers future seven-day
arrivals, not the existing 50,991,223,097-byte native debt. Four days alone leave
about 20.2 GB less than the required post-backlog headroom. Backfill remains held;
any additional lossless relocation needs a separate reviewed capacity calculation.


## Revised capacity plan and temporary reader barrier, 08:06 UTC

A read-only source comparison restricted to 23–24 October found 44 missing native
files, 51,015,441,637 bytes, and no missing or size-different LV1 NetCDF files.
The missing types were 15 LV0, 14 LV0 NetCDF and 15 native LV1. The two additional
small native files explain the change from the earlier 42-file snapshot.

The reviewed plan adds only 13 May to the four days already staging. It contains
64 regular files, 16 of each native type, and 61,415,642,624 allocated bytes. Its
identities were unchanged between 08:04:39 and 08:05:11, with no links, submounts,
open descriptors or process mappings. The latest file mtime was 14 May 01:02:43.
This is a partial historical acquisition day, not a claim of 96-file completeness.
The separate exact-count override preserves all four original 96-file checks.

At 08:06 the projection left 58,176,908,800 bytes on `/data` after all copies, and
658,248,127,771 on `/project` after the known backlog. Those exceed the separate
40 GiB destination reserve and 617,088,629,797-byte future growth target by 15.23 GB
and 41.16 GB respectively. The preparation plan now includes the existing backlog
in its 668,104,071,434-byte primary requirement. It still needs a fresh capacity
check before sequential D13 staging, before cutover and before backfill. This is
plan evidence, not completed copying or a released original.

The raw-reader audit identified three additional launch paths: raw recovery worker,
shared repair and recheck-after-repair. A temporary persistent launch barrier now
covers twelve named consumers in the reviewed code. The recovery coordinator and
its timer are separately paused during the brief barrier; existing product
workers and immutable manifest uploads continue. Live full and raw incremental
inventory commands were verified to enqueue requests only. Regression coverage
shows a condition-skipped raw launch preserves durable debt, epoch and attempt
count across reopening the queue and ordinary replay. A disposable systemd unit
proved held, released and held-again behaviour without touching production readers.
No production relocation hold or mount cutover had been activated for these tests.


## Inactive relocation guards commissioned, 08:14 UTC

PR 85 reached main as `498c94454f0e1518f85a4e2760327ec6f0451a30` after all four
checks and independent review. Its focused preparation reported 12 tasks ok,
four changed and no failures. At 08:14:42, all twelve actual loaded systemd
condition lists contained the negative relocation-hold condition, and no daemon
reload remained pending. The temporary hold and production active record were
both absent. The persistent retention hold remained present.

The original four-day stage retained MainPID 3753660. Retention remained failed
with no process; retention and native backfill timers stayed disabled/inactive.
The live LV1 and recovery timers stayed enabled/active. D03 completed its 96-file
SHA/metadata proof at 08:08:41; its manifest SHA-256 is
`c2ae17045cb110cbd9657f02b1ce945544e3174f51eb8450fd1b390520f30524`.
D04 then began. D13 had not started, and no original or production path moved.

The installed helper SHA-256 is
`632140eaf217bdd78dee6d1623f396a99cb831acf505c6b0420698f934847817`;
the five-day plan SHA-256 is
`59042dd5b155b3d197e1e78ebddb0895f98a10521e0d6b6cab2c1bb95b57d5c6`.
The receipt includes all consumer and launcher states, loaded conditions, sender
processes and locks. Those observations must be repeated at cutover.

Review accepted using fresh, unchanged stage proofs for reversible cutover rather
than rereading the same two copies first. Stage already hashes both sides, compares
metadata and checks whole-day identity maps before publishing its durable proof.
Cutover repeats the identity/file-set and six-hour checks under its exclusive lock.
This avoids an estimated 60–100 minutes and 861.42 GB of repeated reads. Full
verification through the mounted logical paths before any duplicate-original
release remains mandatory and is now an explicit release gate in the helper.


The mounted-proof helper passed a disposable real-bind sequence at 08:22 UTC.
Staging alone and an atomically recreated active record with identical plan JSON
both rejected release. Missing or wrong binds and an interrupted logical read
left the prior proof unchanged. A deliberately wrong mount introduced during the
read was rejected; rollback then restored the preserved original despite having
no usable bind. After restaging, a new full mounted verification allowed release
of only the 56-byte test duplicate while its logical copy remained readable.
Equivalent same-target remounts with unchanged file identities were accepted.
Production stage PID 3753660 stayed unchanged and no production active record was
created. The tiny test trees and receipts remain outside the raw archive.

At 08:22:20 the normal health report had cleared `source_metadata_invalid` in both
recovery and evidence. The earlier label was stale, not a continuing metadata
failure. At 08:22:41 the raw second confirmation was running with no error and one
confirmation remaining; products were still in a fresh inventory with no error.
D02 and D03 had completed staging; D04 had 48.86 GB copied. D05 and D13 had not
started. `/project` had 278,476,619,776 bytes free and `/data` had 249,869,500,416.
These are progress observations, not archive acceptance or completed relocation.
