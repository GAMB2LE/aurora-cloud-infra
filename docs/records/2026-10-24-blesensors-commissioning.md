# BLE commissioning, 24 October 2026

I used the focused BLE playbook to add the missing production source and
appender on JASMIN. The service runs dashboard commit
`ac672ae6d835d05461b2fcb8c209151aa8ace2de` from its separate release checkout,
with `/opt/aurora-presentations/runtime-20261024`. Other scientific writers
keep their existing checkout and runtime.

Infrastructure preparation is in [PR 63](https://github.com/GAMB2LE/aurora-cloud-infra/pull/63).
The first dry run found that the source role would change the shared state
directory's mode. [PR 66](https://github.com/GAMB2LE/aurora-cloud-infra/pull/66)
fixed that before activation; its 28 BLE tests and all four CI checks passed.
The applied infrastructure revision was `500036f7da505a387f2baaae1615391905e76c3a`.

## Archive boundary and preserved settings

Both product archive copies were already running. I paused only their timers
and stopped their copies through the existing systemd stop behaviour. No raw
archive jobs, source files, partial transfers or checkpoints were removed.

| UTC window | Result |
| --- | --- |
| 07:00:37–07:01:49 | Initial idle preflight; both product timers and unfinished copies restored while the shared-directory fix passed CI. |
| 07:03:14–07:04:44 | Final dry run and focused installation; both product timers restored active/enabled and both unfinished copies restarted. |

The saved pre-change state is under
`/var/backups/aurora-blesensors/commission-20261024T070033Z` on JASMIN. Archive
registration kept its own backup under
`/var/backups/aurora-blesensors/20261024T070402Z-9d2b400e`. These protected
backups contain configuration and stay off Git.

The non-BLE environment entries were byte-identical before and after the
release. `/var/lib/aurora-cloud` remained owned by `aurora:aurora`, mode `0755`.
The focused registration added only BLE stream bindings and product exclusions;
it retained the installed archive code and other settings. Both preflight and
registration enforced the product-worker idle gate.

## Production evidence

The first source cycle ran from 07:04:45 to 07:05:43 UTC, received 25 files and
enqueued them through the existing raw archive dispatcher. The initial history
append finished at 07:06:50: 132,096 input records, 66,545 accepted rows and no
rejected records. The resulting store had no duplicate times and observations
through 07:04:54 UTC. Raw files occupied about 8.8 MiB and the product 1.4 MiB.

The next scheduled source cycle finished at 07:06:52, with two changed files
and 23 unchanged files. The scheduled append ran at 07:08:53–07:08:55 and added
114 rows with no rejection. At 07:09:13 the store contained 66,659 unique times;
both APS and ASS temperature observations had advanced to 07:06:50 UTC. A
third source cycle completed successfully at 07:08:57. Both source and append
timers are enabled. The primary inventory now retains the dedicated BLE paths
so a later full deployment uses the same reviewed code and runtime.

## Development mirror evidence

The mirror-only dry run passed, then the focused standby tag installed just
the raw and product BLE stages. Both first transfers succeeded at 07:09:50 UTC;
scheduled checks succeeded again at 07:10:09 and 07:10:24. Both timers are
active/enabled. The existing direct-field continuity timer remains active and
enabled, with its separate data and environment unchanged.

At 07:10:07 on JASMIN and 07:10:17 on data-ocean, the copies agreed:

| Data | Files | SHA-256 over sorted relative paths and file bytes |
| --- | --- | --- |
| BLE raw | 25 | `42dea448785a71d8429c82c61b0955a6588a3fb8b0447ea7a28bfba2c099fcc7` |
| BLE Zarr | 212 | `8686cb6b7eec158a7adb1aa1710b956578b03fef23caa24eb21e46e731547507` |

Both products had 66,659 rows and the same 07:06:50 sensor observations.
Ingestion cursors, writer locks and staging stores were excluded from the
product mirror. These hashes describe this check; later successful cycles will
change them as observations arrive.

## Scope and next checks

This is BLE acquisition/product commissioning, not evidence that all archive
queues are drained or that the operational mobile mirror has passed its full
acceptance gate. The development direct-field continuity service remains a
separate path. Current procedures are in [BLE enclosure data](../BLESENSORS.md).
