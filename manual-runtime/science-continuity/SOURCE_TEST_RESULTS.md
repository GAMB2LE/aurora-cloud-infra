# Source-preservation test result

Command from the isolated infrastructure worktree:

```
/Users/rrniii/Documents/GAMB2LE/output/aurora-stream-recovery-20261023/dashboard/worktree/.venv/bin/python -m pytest manual-runtime/science-continuity/tests/test_seed_chunks.py -q
```

Result on 23 October 2026: **8 passed in 6.03 seconds**. One existing local environment warning was emitted while loading the native-appender fixture: `numpy.ndarray size changed, may indicate binary incompatibility. Expected 16 from C header, got 96 from PyObject`. All scientific equality and native append checks passed. No packages or local environment were changed. The earlier exact-runtime deployed Linux fixture run separately passed all eight tests; its complete output is preserved in `tests-linux.txt`.
