# WXcam recovery, 24 October 2026

The primary raw-camera sync recovered without stopping or replacing its running
copy. I kept the existing camera source, partial files, checkpoint and archive
queue. This record separates that recovery from the small transfer-order
change deployed afterwards.

## Recovery evidence

The mixed JPEG/MP4 batch that started at 06:37:45 was still transferring older
media at 07:12. Its selected list contained 1,154 files: 1,122 JPEGs and 32 MP4s.
The job finished naturally. The next run, 07:13:20–07:13:51, succeeded and
advanced the existing checkpoint to 07:13:20. Further normal cycles also passed.

A source/raw comparison found the latest settled 07:05 JPEGs already present
for both cameras, so no extra copy or service interruption was needed. The
catalogue then advanced naturally. At 07:20:32, its latest JPEG times were
07:10:00 for FISH and 07:10:39 for PANO; both HDR video catalogues reached 06:00.
The eight previously unreadable media entries were unchanged, with no newly
unreadable entries reported. Catalogue freshness does not by itself prove that
a daily video or thumbnail has been rebuilt and mirrored.

The existing reconciliation service had failed at 02:00 with an out-of-space
error. I reran that same service after raw ingestion recovered. It completed
successfully at 07:17:55, after starting at 07:17:53, with exit status 0 and no
pending receipt journal. Its copy-only policy, timer and source paths were
unchanged.

## Focused wrapper release

Infrastructure [PR 71](https://github.com/GAMB2LE/aurora-cloud-infra/pull/71)
merged as `a060cb9972f93faac025b3eda8a89dcba5d784e3`. The new wrapper transfers the
newest three settled JPEGs per camera first, then remaining JPEGs and MP4s.
Each phase has its own 120-second budget. A durable pending-path journal and
unchanged global checkpoint preserve unfinished work across retries, including
files that later age out of the normal scan window. Completed phases must be
accepted by the raw archive queue before the run can finish.

Validation included 15 focused WXcam tests, including old pending media,
timeout/retry, and missing or failing archive dispatch. Required CI run
`40073883834` passed: 522 archive/runtime tests, two skipped tests, 81 forecast
contract tests, all Ansible checks and a strict documentation build.

The focused playbook's dry run changed only the existing wrapper. It was
applied at 07:23:29 with four successful tasks and one changed file. No timer,
source path, archive policy or scientific writer was reconfigured. The rollback
copy is `/usr/local/bin/aurora-wxcam-sync.3756467.2026-10-24@07:23:29~`.

The first normal timer cycle ran at 07:23:45–07:23:48. It transferred the latest
JPEG phase, submitted six paths to the raw queue (one new, five already known),
advanced the checkpoint to 07:23:45 and left no pending transfer journal.
The second normal cycle, 07:25:54–07:25:59, completed the latest and remaining
JPEG phases, queued eight paths (two new, six already known), advanced the
checkpoint to 07:25:54 and again left no pending transfer journal. The existing
source timer remained active and enabled.

[WXcam source sync](../WXCAM_SOURCE_SYNC.md) owns the procedure and rollback
instructions. The successful raw/source runs do not clear independent GWS,
object-store, retention or operational-mirror acceptance gates.

## Android primary contract check

At 07:23:22, the Android app's opt-in JASMIN contract test passed with one test,
zero skips and zero failures. The tested source tree matches Android main
`ed2d0df3d66bbd4a457a4fa9987abc615075682d`. It decoded health, schema-3 manifest,
Overview, Operations, current and forecast Power, Quicklooks, station cameras,
UAS, WXcam and archived UAS-flight payloads, and fetched representative
quicklook, camera-preview and flight-plot media. The Gradle run completed in
17 seconds.

This checks the live API contract and media access. It does not establish fresh
Menapia flight acquisition, a signed app upload or installed-device behaviour.
The separate public operational-mirror contract test remains held until that
route is commissioned; the private API tests have their own presentation record.
