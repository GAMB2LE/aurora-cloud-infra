# Manually deployed science continuity worker

This directory preserves the exact worker deployed during the 23 October 2026
HATPRO recovery. It is an operational source record, not a managed Ansible role.
No playbook or task installs it, and checking out this commit changes no running
service. Existing service/timer ownership and deployment coordination remain
with the operator.

The runtime is `/usr/local/lib/aurora-science-continuity.py` on the DigitalOcean
standby. Its SHA256 is
`35aabfbd0159b91b80d20efa7f63e82ce85bdb98639d38cb95d709ef77fa8377`.
That content hash identifies the deployed file. The Git commit containing this
directory separately identifies its source preservation. The dashboard catalog
commit recorded in `PROVENANCE.json` is a third, separate change that exposes
failed HATPRO publications in the UI; it does not contain this worker repair.

## Repair boundary

The pre-repair runtime is retained only as `tests/before.py`, SHA256
`f0753a520eb45507c5c2dacebcf6c07257244ce7bd37e2a25983aae18d65262f`.
It already includes the earlier bounded shared lock and CL61 catch-up changes.
Do not treat that fixture as a deployment candidate.

`worker.patch` contains the complete repair against that exact runtime. Only
the embedded `SEED_PRODUCT` serialization changes. The trimmed dataset is
rechunked, inherited per-variable `chunks` encoding is removed, and the fresh
store is written with `safe_chunks=True`. This aligns the destination Zarr grid
with the new Dask grid instead of inheriting a conflicting 297-sample grid.
Scientific values, coordinates and attributes are unchanged. Worker source
routes, guards, locks, phase budgets, publication behavior and WXcam code are
unchanged, and no excluded field-met/ASFS processing is added.

The shared seed code is used by HATPRO, CL61 and radar. The unchanged deployed
HATPRO native appender and its `rebuild_cutoff` dependency are retained under
`tests/` as fixtures so the next append can be tested against the exact runtime
used for the repair. These copies are not new processing implementations and
are not installed by this repository.

## Focused validation

Use the dashboard Python environment with numpy, xarray, Dask, Zarr v2,
numcodecs and netCDF4 available. The tests can run with either pytest or their
standard-library runner:

```
python -m pytest manual-runtime/science-continuity/tests/test_seed_chunks.py -q
python manual-runtime/science-continuity/tests/test_seed_chunks.py
```

The eight tests reproduce the old 297/300 chunk failure; check exact retained
values, coordinates, attributes and unchanged source bytes; exercise 1D/2D
arrays with range chunks 48/46; run a real native append with overlapping
samples and another trim; and cover CL61/radar, untrimmed copies and empty
windows. An AST comparison proves the rest of the worker is unchanged.

`tests-linux.txt` records the eight successful fixture tests under the deployed
Linux interpreter in isolated temporary storage, with no dependency installs
or production jobs. `INDEPENDENT_REVIEW.md` records the separate repair review.
The source copy of the tests differs only in locating the worker one directory
above the fixtures. `PROVENANCE.json` records the exact original and copied
file hashes and source baseline.

Live acceptance is separate from fixture tests. The repaired worker first
published at 13:00:24 UTC, advancing HATPRO samples from 09:44:55 to 12:44:56.
A normal scheduled publication at 13:13:39 advanced the rolling cutoff with
all 155,440 retained timestamps across six variables exactly unchanged. Both
native LWP/IWV sample values and field-file hashes were verified, and actual
public PNG/API responses were checked. The same-hour second publication proves
cutoff stability; a later distinct hourly frontier is recorded in the incident
acceptance evidence separately.

Any future rollout must compare the current deployed hash before replacement,
preserve subsequent changes, coordinate with active workers, and verify normal
publications afterward. This source record neither authorizes a rollout nor
provides an automatic deployment command.
