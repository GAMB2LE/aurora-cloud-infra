# HATPRO seed repair review, 23 October 2026

The historical independent review found no blocker for candidate SHA-256
`35aabfbd0159b91b80d20efa7f63e82ce85bdb98639d38cb95d709ef77fa8377`
against baseline `f0753a520eb45507c5c2dacebcf6c07257244ce7bd37e2a25983aae18d65262f`.
This is that dated result, not a review of later installed code.

The fix removes inherited variable chunk encoding after trimming and rechunking
the seed. It keeps `safe_chunks=True`; the old 297-sample encoding cannot be
reused with the new 300-sample grid. Scientific values and methods stay unchanged.

Both independent runs passed all eight tests: pytest in 5.75 seconds and the
stdlib runner in 5.670 seconds. The recorded deployed-Linux fixture run also
passed eight in 34.867 seconds. Logs remain in this directory.

The tests cover the original failure, retained values/coordinates/attributes,
48/46 profile chunks, source bytes, native append overlap, next trim, empty
windows and CL61/radar grids. AST comparison checks that routes, guards, locks,
budgets and other embedded commands are unchanged. The reviewer made no live
change or production run. A future deployment still needs an exact installed
hash check and coordinated ownership.

The [original review](https://github.com/GAMB2LE/aurora-cloud-infra/blob/7f25708/manual-runtime/science-continuity/INDEPENDENT_REVIEW.md)
remains in Git. See [the worker guide](README.md) for the preservation boundary.
