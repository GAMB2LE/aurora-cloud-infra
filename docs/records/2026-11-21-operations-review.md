# Operations code review, 21 November 2026

I saved and pushed the pending operations work, merged the 23 newer commits
from `origin/main`, then reviewed the integrated changes and documentation.
The first checkpoint was `379683a`; upstream integration was `7f25708`.
This review changes repository source. It does not deploy the new review fixes.

## Findings fixed

| Finding | Change and regression |
| --- | --- |
| A full template release could replace a storage-guarded verifier or health entrypoint. | Read-only preflight now refuses guarded programs, unsafe files and symlinks, including in check mode. The patch renderer also refuses a wrapper instead of treating it as the payload. |
| Fresh lanes required the historical coordinator to remain active after its work finished. | The completion gate accepts only its exact successful service identity, completed frozen cohort and all 581 paired obligations. Failed/incomplete work still blocks admission. |
| Prior GWS receipts overwrote the saved object-isolation proof in `known_paths`. | Separate variables preserve the proof. Tests cover an ordinary prior receipt, stable isolation and a changed proof. |
| Forecast CI selected Python 3.14 and could not build pinned `numcodecs`. | Pin the forecast job to the locally verified Python 3.13 environment; production archive checks remain on 3.10. |
| SSH policy checks assumed lowercase `sshd -T` keys. | Normalize keys on both the role and isolated policy tests; verify another user does not inherit tunnel restrictions. |

The hourly native radar allowance remains pending for 75 minutes from the last
source write. Raw discrepancy counters remain visible; expiry, mismatches and
invalid evidence still alert. No retention gate was relaxed.

## Documentation

I reviewed all 50 existing Markdown pages. The revised pages use shorter practical
instructions, shared setup links and explicit source/product/archive boundaries.
Existing documentation is about 36% smaller by bytes. Detailed historical records
remain available through immutable Git links. Necessary copy, verification,
capacity, interruption and rollback checks remain in the operator procedures.

Corrections include active flat HATPRO paths, BLE and Menapia product locations,
the recovery schedule, and the historical Menapia credential expiry. A dated
access failure is not presented as today's live state.

## Checks

- Local suite: **968 passed, 23 skipped, 271 subtests passed** on macOS/Python 3.13.
- All 37 playbooks passed syntax checks; inventory and strict MkDocs build passed.
- Changed Python files parsed with Python 3.10 syntax. Ruff's undefined-name and
  syntax checks passed across `roles` and `scripts`; whitespace checks passed.
- Secret-pattern scan found no private key or common token pattern in the pending
  files. This is a bounded scan, not a guarantee that every possible secret is absent.
- Bandit found no high-severity result in the new radar/edge helper scan. Its 24
  medium results were reviewed: four fixed-identifier SQL queries, 19 guarded
  payload executions and one intentional group-writable GWS file publication.

The PR's `validate` workflow supplies the Linux/Python 3.10 runtime, forecast,
Ansible and documentation results. Its privileged cross-UID test must execute
rather than skip. Local macOS skips do not replace those checks.

## Release boundary

The radar recovery directory preserves versioned host helpers; it is not an
installable Ansible role. Exact host paths, imported helpers, frozen manifests,
worker hashes and queue schemas need their own release review. Some operational
payloads remain host-managed. A broad role apply is not a deployment plan for them.

Before any host release, inspect the installed guard/payload and live jobs, then
verify a fresh independent archive readback and public health. This source review
makes no claim that live queues are empty, all archives match or pruning is safe.
