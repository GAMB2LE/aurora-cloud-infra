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
2.61 TB free during this check; SSH access and Linux disk quota did not cause the
missing estimate.

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
