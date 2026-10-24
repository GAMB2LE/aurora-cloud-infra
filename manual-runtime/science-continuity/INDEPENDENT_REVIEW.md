# Independent HATPRO seed repair review

No blocker found for candidate SHA256 `35aabfbd0159b91b80d20efa7f63e82ce85bdb98639d38cb95d709ef77fa8377` against exact live baseline `f0753a520eb45507c5c2dacebcf6c07257244ce7bd37e2a25983aae18d65262f`.

The patch changes only the embedded rolling-window seed serialization. After selecting the retained time window and rechunking its arrays, it removes inherited per-variable `chunks` encoding so the new store uses the new aligned Dask chunk grid. It explicitly preserves `safe_chunks=True`. The old 297-sample Zarr encoding is not valid for the new300-sample grid; bypassing the chunk-safety check is neither required nor used.

Independent pytest run:8 passed in5.75s. After conversion to the deployed-Python-compatible stdlib runner, independent unittest run:8 passed in5.670s. Logs are `independent-tests.txt` and `independent-unittest.txt`. Author also reports the identical eight tests passed on deployed Linux Python in34.867s (`tests-linux.txt`).

Tests reproduce the original297/300 chunk-overlap failure, compare all values/coordinates/attributes after trim including94 profile levels split48/46, preserve source bytes, exercise the unchanged deployed HATPRO appender with native NetCDF fixtures and overlap/deduplication, verify subsequent append and next trim, preserve untrimmed byte-copy behavior and empty windows, and cover CL61/radar seed-compatible chunk grids. AST comparison confirms all worker guards, source routes, locks, budgets, publication behavior and other embedded commands remain unchanged.

This is a storage-serialization fix, not a scientific algorithm or calibration change. No excluded ASFS/CRD/met processing is added. Runtime deployment still requires exact live hash preflight and coordinator ownership. This reviewer made no live changes and did not invoke a production worker.
