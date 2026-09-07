# Archive recovery testing

The `archive-runtime` job in `.github/workflows/validate.yml` runs the archive
programs on Python 3.10 with the same hash-locked Boto3 dependencies installed
on the production host. The Ansible inventory/playbook validation and strict
documentation build remain separate required checks.

## Run the automated checks locally

Use a separate Python 3.10 environment; the Ansible control project uses a
newer Python version and is not the production archive runtime.

```sh
recovery_test_env=$(mktemp -d /tmp/aurora-archive-tests.XXXXXX)
python3.10 -m venv "$recovery_test_env"
"$recovery_test_env/bin/python" -m pip install --require-hashes --only-binary=:all: \
  -r roles/object_store_mirror/files/aurora-object-store-recovery-requirements.txt
"$recovery_test_env/bin/python" -m pip check
"$recovery_test_env/bin/python" -m compileall -q roles/object_store_mirror/files
"$recovery_test_env/bin/python" -m unittest discover -s tests -p 'test_*.py' -v
```

These tests use temporary local files, fake gateway responses and mocked copy
commands. They do not require production credentials, contact JASMIN, or prune
instrument data. Full discovery keeps the existing inventory, repair, recheck,
retention, health presentation and service-unit contract tests in the same run
as the new recovery tests.

## Recovery scenarios covered

| Area | Required evidence |
| --- | --- |
| Paged S3 reader | Late 504s preserve earlier pages and resume at the committed cursor; invalid tokens reset only their shard; malformed pages, corrupt checkpoints, conflicting replays and expired observations cannot certify an archive. |
| Persistent coordinator | Raw capacity remains available during product failures; retry cooldowns survive reopen; daily audits require fresh coverage of every configured family; distinct confirmations cannot be counted twice. |
| Repair and publication | Repair invalidates the collecting generation before copies; competing queue writers cannot overwrite that invalidation; publication merges the latest family state and keeps readers pinned through cleanup. |
| Resource and credential boundaries | Checkpoints, queue journals and source/artifact files respect recovery storage limits; credentials come from the nominated restricted remote; the SDK cannot perform nested request retries. |
| End-to-end failure/recovery | Four families finish while raw and products fail on a late page; recreated workers resume their checkpoints, then collect separate clean confirmations without repeating successful families. |
| Health and acceptance | Automatic retry remains explanatory while valid evidence exists; expiry, permanent failure and missing coordinator heartbeats alert; acceptance rejects settled cloud/GWS discrepancies, invalid counter evidence and legacy policy credit, and requires the configured duration and subsequent completed daily audits. |
| Incomplete source copies | Zero, negative and non-finite source mtimes cannot enter settled verification, resume from a frozen checkpoint or authorize exact repair. Source-metadata faults remain visible through retries and reject acceptance even with clean cached evidence; normal gateway retries retain their existing quiet policy. |
| HATPRO path discovery | Old-mtime files and whole-directory relocations are discovered; failed listings/copies/enqueues preserve pending handoffs and cursor safety; source-path moves, explicit fresh-start baselines, corrupt/configuration-mismatched state and shared backfill lock contention are covered. Existing copy flags and timer schedules are preserved. |

## Live compatibility and acceptance are separate

`scripts/probe_archive_pagination.py` is a bounded read-only JASMIN probe. Its
checks use five serial read requests: two recursive pages, delimiter discovery,
a nonempty terminal prefix and an empty random prefix. It validates continuation,
distinct keys, KeyCount, object sizes and timezone-aware timestamps. This probe
does not by itself prove process restart or full family coverage. Cover those
separately in the isolated shadow collector. Run it using the restricted
service credential; never place credentials in a command line or test fixture.

A successful probe or CI run does not demonstrate complete live archive
coverage. Deployment must also run the new collector in an isolated shadow
catalog, compare settled source/GWS/S3 results, and pass the automated
post-deployment observation for at least 48 hours and two completed daily
audit cycles. Fault injection belongs in the local tests or isolated shadow
environment; production archive objects and retention evidence are not test
fixtures.
