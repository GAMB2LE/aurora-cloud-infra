# Operations collector release

I use this release when the operations snapshot needs a fix while the scientific
writers keep their existing code. It updates only the primary collector's
working directory and command. Its environment files, sandbox, output paths and
timer stay in place. The next scheduled run uses the new command; the playbook
does not start, restart or enable a service or timer.

The development collector is outside this release. The primary collector already
probes both public dashboard URLs, so this does not change temporary development
continuity or enable a second collector.

## Prepare and check

Use a tested 40-character dashboard commit reachable from `origin/main`. Stage a
clean checkout at `/opt/aurora-presentations/<commit>` and a separate Python 3.12
runtime under `/opt/aurora-presentations/`. The tested presentation runtime can
be reused. Do not change the scientific writers' checkout or Python environment.
Refresh the staged checkout's `origin/main` before running the release.

The focused collector tests are `test_operations_http_probe.py`,
`test_enclosure_sensors.py` and `test_operations_storage_paths.py` in the dashboard
repository. Its normal CI remains the release gate. The role checks the exact
commit, clean source, main ancestry, isolated runtime, module import paths and
the presence of the existing collector unit before writing its override.

Record the current `systemctl cat aurora-ops-monitor-collect.service` and
`systemctl show aurora-ops-monitor-collect.service` output. Check that the
existing `--output-root` and `--health-output-root` match
`aurora_paths.raw_ops_monitor` and `aurora_paths.products_ops_monitor_health` in
the selected inventory. Keep any existing environment and sandbox overrides.

Set these explicit release variables in a local YAML file:

```yaml
aurora_operations_collector_revision: "REPLACE_WITH_TESTED_40_CHARACTER_MAIN_COMMIT"
aurora_operations_collector_venv: "/opt/aurora-presentations/REPLACE_WITH_TESTED_RUNTIME"
```

Run the focused playbook against the production primary:

```bash
uv run ansible-playbook playbooks/operations_collector_release.yml \
  --limit aurora-cloud --check --diff -e @/tmp/operations-collector-release.yml
uv run ansible-playbook playbooks/operations_collector_release.yml \
  --limit aurora-cloud -e @/tmp/operations-collector-release.yml
```

The role rejects non-production and non-primary hosts. It writes the source and
runtime record to `/etc/aurora-operations-collector-release.json`, backs up an
existing override, validates the assembled unit and reloads systemd. It does not
reset `EnvironmentFile`, change the presentation provenance file or run a probe
that writes a snapshot during preflight.

## Verify and roll back

Confirm the effective command uses the selected source/runtime and the same two
output paths. Check that environment, sandbox and timer state match the recorded
values. Read-only HTTP probes should report `bootstrap_ready_state` and its
legacy `full_document_state` alias consistently. A ready HTTP bootstrap is not
proof that a browser rendered the page.

Wait for the normal timer run, then check its exit status and the new snapshot's
timestamp, readiness fields and output locations. Check browser rendering
separately. Do not describe installation of the drop-in as a completed live check.

For rollback, restore the saved
`zz-operations-collector-release.conf` and release record. If this was the first
collector override, remove only that drop-in. Run `systemctl daemon-reload` and
verify the next scheduled run. Leave the original service, timer, environment
files and other drop-ins in place, and record the rollback with the release.
