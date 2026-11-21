# Archive tests

I run the whole offline test suite before reviewing a release. CI separates
Python 3.13 forecast tests, the production Python 3.10 archive runtime, Ansible checks and
the strict documentation build.

```bash
uv sync --locked --group test
uv run --no-sync python -m pytest -q tests
uv run --no-sync ansible-inventory --list >/dev/null
uv run --no-sync --with-requirements requirements-docs.txt mkdocs build --strict
```

The production archive dependencies are hash-locked in
`roles/object_store_mirror/files/aurora-object-store-recovery-requirements.txt`.
Use an isolated Python 3.10 environment to reproduce the `archive-runtime`
job. The newer Ansible control environment is not the production runtime.
CI also compiles the radar recovery and edge-status helpers.

## What the tests must prove

| Area | Check |
| --- | --- |
| Collection and retries | Pages and cursors survive restart; bad tokens reset one shard; malformed listings and invalid source mtimes cannot certify an archive |
| Publication | Readers see one pinned report/gate generation; repair invalidation survives concurrent writers; lock contention does not invent fresh evidence |
| Delivery | Durable intent precedes launch; unresolved work is reconciled before another attempt; GWS and object receipts bind the exact file version |
| Paths | Active flat HATPRO paths remain required; relocations retain exact relative paths; symlinks, unsafe names and changed files fail explicitly |
| Alerts | Normal native radar delivery is pending; missing paths alert after 75 minutes; mismatches, expired grace and malformed evidence remain visible |
| Safety | Retention needs its own fresh signed proof; no recovery receipt grants deletion authority; guarded entrypoints cannot be overwritten by plain templates |
| Acceptance | Two distinct confirmations, current independent proof, storage limits and the full unattended observation window remain required |

The guarded-program tests check both the public wrapper and the executing
pinned payload. The historical-coordinator regression accepts an inactive
service only after the exact frozen cohort is complete. SSH-policy tests use
an isolated configuration and verify that restrictions do not affect another user.

## Live checks

`scripts/probe_archive_pagination.py` makes five bounded, read-only JASMIN
requests. Run it with the restricted service credential; never put credentials
in command arguments or fixtures. A successful probe is not a complete inventory.

Before claiming a repair worked, check the installed payload, a fresh
independent scan, the actual source/product frontiers and the public API.
A passing local test is not deployment evidence. Full recovery acceptance
still needs its separate 48-hour window and two completed daily audits.
