# Primary restoration: source consolidation, 24 October 2026

This records source changes and local validation. It does not claim that a
server was recovered, a mirror caught up, or an app beta became available.
Live verification and release evidence must be added after those steps finish.

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

The implementation record must still supply the merged remote `main` commit,
required CI results, presentation and writer revisions on both hosts, source
and mirror timestamps, storage/quota measurements, archive gates, public docs
publication and native beta status. Until recorded, those outcomes are unverified.
