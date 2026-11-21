# Primary restoration, 24 October 2026

I restored the live radar feed on JASMIN and kept data-ocean as a selected
read-only mirror. This is a dated record, not a current health report.

## What was checked

- Infrastructure PRs 59–60 merged after all four checks passed. The integrated
  local suite passed 544 tests and 247 subtests; the privileged Linux test was
  left to CI. All playbooks, inventory and the strict docs build passed.
- A quoted-wildcard fix recovered the 05:30–06:20 UTC radar window: four native
  files, 3,683,680,202 bytes. The LV1 checksum matched the source, the append
  completed and the public API showed the same observation time.
- The live timer was restored at 07:01 UTC with LV1 NetCDF only. Thirteen older
  LV1 NetCDF files were recovered separately without advancing its checkpoint.
  All 15 checked source/cloud LV1 files matched SHA-256.
- The next scheduled transfer and append advanced the product/API to
  `2026-10-24T06:59:52.536Z`.
- The relocation helper and sender guards were installed separately. Disposable
  mount tests rejected missing/changed files and missing binds. Stopping a test
  bind stopped its consumer. A supervising parent retained the sender lock
  across `sudo`, including exit and signal handling.
- D02 completed its 96-file stage at 07:44:20. Staging D03–D05 continued.
  No production relocation was activated in this record.

## What remained open

The source was producing about 84.86 GB of native radar data per day. The dated
seven-day requirement was 617.09 GB, above the available primary headroom.
Native backfill and ASS retention stayed disabled; their failed service states
were preserved. The live LV1 timer stayed enabled.

Receiving old raw files did not repair the published product gap between
`2026-10-23T14:59:51.910` and `2026-10-24T04:59:59.854`. That needs a separately
verified candidate and controlled publication. No product rebuild was performed.
The data-ocean mirror kept its `Y2026/**` and `.radar-partials/**` exclusions;
it did not become a full native radar archive.

The [later archive follow-up](2026-10-24-archive-follow-up.md) records all five
stages and the decision to defer cutover. Use the
[radar guide](../RADAR_SOURCE_SYNC.md) and
[relocation procedure](../RADAR_RAW_RELOCATION.md) for new work.

The [full original record](https://github.com/GAMB2LE/aurora-cloud-infra/blob/7f25708/docs/records/2026-10-24-primary-restoration.md)
retains the exact commits, installed hashes, measurements and receipt paths.
