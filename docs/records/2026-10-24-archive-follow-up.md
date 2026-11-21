# Archive follow-up, 24 October 2026

I checked the archive blockers during primary restoration. All five radar days
finished staging, but I deferred cutover because sustained `/data` headroom
was unresolved. This is a dated result, not permission to resume retention.

## Archive findings

- Normal delivery repaired the ASFS file named by the old report. Its source
  and cloud SHA-256 matched; the aggregate report was still catching up.
- Three empty, zero-mtime HATPRO placeholders had no matching flat source path.
  Their complete dated counterparts matched the source. I preserved the empty
  files by rename under the shared HATPRO lock and requested a fresh raw check.
  No observation timestamp was invented.
- Old object-store counts were stale: sampled objects were already present.
  Fresh complete comparisons were required before estimating repair debt.
- A raw copy hit rclone's temporary-camera-file race. A blanket exit-6 success
  rule would hide real errors; no receiver fix was deployed in these checks.
- ASS storage-health metadata was stale because its source producer was absent.
  Current science files were still arriving. Fetching the old JSON successfully
  did not make its capacity estimate current.

## Staging and deferred cutover

D02–D05 and the explicitly partial D13 finished staging: 448 files and
430,711,452,160 allocated bytes. SHA proofs and fresh identities passed. Originals
remained at their existing paths; there was no quarantine rename, bind mount,
mounted-verification receipt or duplicate-original release.

At 09:22:53 UTC, `/data` had 60,023,238,656 bytes available and `/project` had
278,258,515,968. Native debt was 58,263,009,908 bytes. The short copy/drain window
passed the 50 GiB `/data` floor, but did not establish sustained headroom.
`/data` already occupied its whole 999 GiB device. Reserved filesystem blocks
were left unchanged. Releasing primary originals would free `/project`, not
solve `/data` growth, so I held the cutover.

The owned temporary barrier was removed after unchanged checks. Eight primary
timers were restored at 09:29:52 and the selected DO radar timer at 09:31:26.
Recovery and shared repair replayed successfully. Nothing was force-stopped or
reset. Native backfill and retention stayed disabled/inactive, with the separate
persistent retention hold present.

## Final readback, 09:32 UTC

- Live sync, append and quicklooks completed; the product frontier remained
  08:59:52.150 UTC because that cycle had no newer complete candidate.
- The DO selected mirror succeeded with no deletion or changed bytes.
- Raw object copying completed at 09:27:21. GWS raw transfer was still retrying
  receiver rename failures; it was not complete.
- Raw verification had two clean independent confirmations. Products' second
  confirmation was still running. Unattended acceptance remained under review.

The host receipts are `cutover-20261024T092315Z-barrier.json` under
`/var/lib/aurora-cloud/radar-relocation-20261024/` on primary and the matching
`cutover-20261024T092315Z-ocean.json` on DO. Keep the originals and staged copies.
A later cutover needs fresh capacity, closure and stage proofs; release still
requires full mounted verification.

The [full original record](https://github.com/GAMB2LE/aurora-cloud-infra/blob/7f25708/docs/records/2026-10-24-archive-follow-up.md)
retains all measurements, hashes and intermediate checks. Current procedures
are in [archive recovery](../ARCHIVE_RECOVERY.md) and
[radar relocation](../RADAR_RAW_RELOCATION.md).
