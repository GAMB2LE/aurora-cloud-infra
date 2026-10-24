# Releases from main

`main` is where completed code and documentation meet. Neither server follows
its moving tip. Record the infrastructure commit, presentation commit, writer
commit and app build separately; they may deliberately differ.

## Keep presentation and science releases separate

The original `/opt/aurora-cloud-dashboard` is also used by scheduled scientific
writers. Changing that checkout changes what they run next, even if a playbook
only restarts web and API processes. The existing code-release playbooks
therefore require `aurora_shared_writer_source_change_reviewed=true`.

For presentation-only recovery, install the tested `main` commit in a separate
`/opt/aurora-presentations/<full-commit>` checkout with its own pinned runtime
virtual environment using Python 3.12 or newer. Keep the existing scientific
writer virtual environment unchanged; a presentation dependency update must
not upgrade that environment. Point only the dashboard and mobile API unit overrides at
that checkout. Preserve their existing environment files, sandbox settings and
runtime overrides. Do not change the writer checkout, forecast revision or
writer units. Review `systemctl cat` before applying overrides; reset only
`WorkingDirectory` and `ExecStart`, and record the previous values for rollback.

The focused `playbooks/presentation_release.yml` applies these two reader
overrides and verifies the API revision. Stage a normal clean clone at the
commit path and fetch `origin/main`; the playbook checks that the release is
reachable from it. Install the pinned runtime requirements in a separate
environment first. Put the full revision and runtime path in a release file:

```yaml
aurora_presentation_revision: <40-character commit on main>
aurora_presentation_venv: /opt/aurora-presentations/runtime-<release-date>
```

Review the effective units and retain them with the release evidence. On a
host using direct-field continuity, also set
`aurora_presentation_wxcam_media_dir` and
`aurora_presentation_auroracam_media_dir` to the currently served paths. The
playbook preserves existing environment overlays but cannot infer static
media routes from them.

```bash
uv run ansible-playbook playbooks/presentation_release.yml --limit <host> -e @/path/to/presentation-release.yml --check --diff
uv run ansible-playbook playbooks/presentation_release.yml --limit <host> -e @/path/to/presentation-release.yml
```

The `zz-presentation-release.conf` drop-ins take precedence over older command
overrides. `aurora-presentation.env` sets only reader provenance and disables
Python bytecode writes; it does not set forecast-generator provenance. Check
the browser and API after release. To roll back the first isolated release,
remove only those two drop-ins, reload systemd and restart the two readers.
For later releases, restore their recorded previous drop-ins and presentation
environment. Keep the release checkout and runtime until rollback is no longer
needed. A failed runtime or manifest check is a failed release.

The operational mirror uses dedicated `aurora_operational_mirror_app_dir`
and `aurora_operational_mirror_venv` settings. Never repoint the shared
`aurora_app_dir` or `aurora_venv` to change a reader. See
[Operational mobile mirror](OPERATIONAL_MOBILE_MIRROR.md).

The release also gives the existing operations collector the presentation
provenance file. Its next scheduled snapshot then reports the checkout serving
the website. This does not change the collector command or the scientific writer
checkout. I keep those separate so an older writer pin is not mistaken for the
public dashboard version.

Before releasing, preserve dirty files and deployed-only fixes in a reviewed
branch. Build integration worktrees from current `origin/main`. Merge completed
work through the repository checks, preserve unfinished experiments on named
branches, and do not force-push. Run checks again on the merged revision.


Branches and tags:

- `main`: the shared integration branch for reviewed code and documentation.
- Exact commits: independently pinned development and presentation releases.
- `prod-YYYYMMDD.N`: annotated production release tags.

Promotion sequence:

1. Merge reviewed changes into `main`, run the integrated checks, and record the full commit.
2. Deploy that exact candidate to data-ocean.
3. Run smoke tests on `https://data-ocean.gamb2le.co.uk/app`.
4. Confirm data-ocean shows the development banner and live mirror lag.
5. Create an annotated `prod-*` tag on the validated `main` commit.
6. Deploy exactly that tag to JASMIN.
7. Smoke-test `https://data.gamb2le.co.uk/app`.

Ansible refuses to deploy over a dirty checkout. Preserve unexpected host
changes as a patch/tag, clean the checkout, and deploy the exact inventory ref.
Controller-side source overlays and in-place edits are not part of the release
process.

For a code-only staging or production release, use the focused playbook so
source sync, storage, networking, and replication roles are not changed:

```bash
uv run ansible-playbook playbooks/dashboard_release.yml --limit <host> --check --diff
uv run ansible-playbook playbooks/dashboard_release.yml --limit <host>
```

For a source-only fix shared by the Panel dashboard and mobile API, use
`playbooks/dashboard_code_release.yml` with full
`aurora_code_release_expected_revision` and `aurora_code_release_target_revision`
values, plus the exact `aurora_app_version` (an annotated production tag or the
development target SHA). It applies the same clean-checkout and release-tag
guards as the mobile-only procedure below, compiles changed modules using the
existing environment, and restarts only `aurora-dashboard.service` and
`aurora-mobile-api.service`. It verifies both the API and the dashboard's page
response after release. It does not install packages or apply service units,
nginx, timers, or data roles; it updates only forecast source-revision metadata
in the existing environment file. Browser render and interaction checks remain
required because an HTTP response does not prove that the UI rendered.

When the two hosts intentionally run different revisions, base each fix on its
host's current SHA. Validate the exact production candidate in a temporary,
loopback-only process on development before tagging it, so a hotfix never
implicitly promotes unrelated development code.

For an existing mobile API that needs only a Python source correction, use
`playbooks/mobile_api_code_release.yml`. It checks out an exact reviewed commit,
compiles changed Python files in memory with the existing virtual environment,
and restarts only `aurora-mobile-api.service` when the source SHA changes. It
does not install dependencies or apply service units, environment files, nginx,
timers, or data-product roles. Use a candidate based on the host's current
source so unrelated development work remains included; inspect that complete
diff before release. Do not use this path for dependency or configuration changes.

Supply a reviewed variables file containing the current host SHA, candidate SHA,
and release ref:

```yaml
aurora_mobile_api_expected_revision: <full current host commit SHA>
aurora_mobile_api_target_revision: <full candidate commit SHA>
# Development: the same candidate SHA. Production: an annotated prod-YYYYMMDD.N tag.
aurora_app_version: <exact development SHA or production tag>
```

```bash
uv run ansible-playbook playbooks/mobile_api_code_release.yml --limit <host> -e @/path/to/release-vars.yml --check --diff
uv run ansible-playbook playbooks/mobile_api_code_release.yml --limit <host> -e @/path/to/release-vars.yml
```

Both modes require an existing clean checkout at the expected SHA, the configured
origin, an existing Python environment, and a healthy local API. Production also
requires the remote annotated tag to resolve to the candidate SHA. Check mode
performs these read-only checks and predicts the Git change; it does not fetch,
compile the candidate, restart, or validate candidate health. A normal run checks
`http://127.0.0.1:<mobile API port>/health` after restart. Verify the public API and
the corrected plots separately before claiming release acceptance. Record any
required inventory pin update separately from this narrowly scoped deployment.
The Git task suppresses patch output even with `--diff`: Ansible's Git diff
implementation fetches objects during check mode. Review the candidate patch
in the source repository before running the host checks.

Use the runtime release playbook when preparing or repairing the complete
dashboard service set, including source sync, nginx, and development mirror
units. It deliberately does not reapply GWS, object-store, verification,
archive-monitoring, or retention services:

```bash
uv run ansible-playbook playbooks/dashboard_runtime_release.yml --limit <host> --check --diff
uv run ansible-playbook playbooks/dashboard_runtime_release.yml --limit <host>
```

The runtime playbook assumes the host baseline, storage, and network roles have
already been provisioned. Run `playbooks/site.yml` separately for those host
baseline changes; its check mode can report package/service ordering failures
when a package is absent and would only be installed during the same run.
Apply archive services independently with `playbooks/archive_services.yml`.

Do not deploy untagged experimental changes directly to production.

Every release snapshot now contains a checksum manifest and the pre-release
state of the affected dashboard/Power timers. To restore development, pass the
exact snapshot directory name and the exact previous 40-character dashboard
commit. The playbook refuses production, requires that commit to equal the
checksummed snapshot `source_commit`, quiesces and verifies every named Power
writer/evaluator plus both serving processes, restores configuration, checks
out the old revision, synchronizes its pinned runtime dependencies, restores
the captured timer states, and never modifies product data:

```bash
uv run ansible-playbook playbooks/dev_dashboard_rollback.yml \
  -e aurora_rollback_snapshot_name=YYYYMMDDTHHMMSSZ \
  -e aurora_rollback_dashboard_revision=<40-character-commit>
```

Before the first v12 machinery deployment, freeze the existing development v10
evidence with a separate, explicit one-shot playbook. The snapshot name is
immutable and cannot be reused:

```bash
uv run ansible-playbook playbooks/power_v10_baseline_snapshot.yml \
  --limit aurora-cloud-droplet --check --diff \
  -e aurora_power_baseline_snapshot_name=v10-pre-v12-YYYYMMDD
uv run ansible-playbook playbooks/power_v10_baseline_snapshot.yml \
  --limit aurora-cloud-droplet \
  -e aurora_power_baseline_snapshot_name=v10-pre-v12-YYYYMMDD
```

This operation is accepted only while development publication remains off. It
temporarily quiesces the Power-derived writers, holds the shared deterministic
generator lock, copies a literal allowlist of deterministic, ensemble,
planning, operating, and display evidence, records any available CL61
diagnostic-only evidence, and restores the exact prior unit state on every exit
path. It also records checksummed installed unit/drop-in definitions and the
clean capture-checkout revision; product-internal provenance remains the
authority for the code that originally generated each artifact. The copied
source and destination bytes are NUL-framed SHA-256 checked, atomically renamed into
`/var/lib/aurora-power-baseline-snapshots/<name>/`, and made root-owned and
non-writable. It excludes raw observations, mirrored Power/PDU input, ECMWF
caches and global grids, temporary retrievals, candidates, evaluation outputs,
and forecast-bundle staging. Only allowlisted non-secret Power settings are
retained; the routine release snapshot separately preserves the full root-only
configuration. The evidence snapshot is not an automatic rollback source and
never alters product data.

## Required Approval

Get explicit user approval before changing any of these:

- writer timers or host role changes
- source-sync logic
- Zarr, SQLite, or schema migrations
- production raw/product paths
- nginx, DNS, or certificates
- alert recipients or routing
- secrets, SSH, Tailscale, or credentials
- destructive cleanup or rollback affecting data

Low-risk dashboard-only bug fixes can be released after staging checks pass.

## Preflight Before Writer Cutover

Before enabling production writers on JASMIN and disabling data-ocean writers,
capture state and verify access:

```bash
sudo systemctl list-timers --all 'aurora-*'
sudo systemctl --failed --no-pager
sudo -u aurora git -C /opt/aurora-cloud-dashboard status --short --branch
curl --fail --silent --show-error --output /dev/null --write-out '%{http_code}\n' https://data.gamb2le.co.uk/app
curl --fail --silent --show-error --output /dev/null --write-out '%{http_code}\n' https://data-ocean.gamb2le.co.uk/app
```

JASMIN must be able to reach the ASS/APS source hosts and GWS transfers before
production writer timers are enabled there.

## Staging Checks

On data-ocean:

```bash
sudo systemctl is-active aurora-dashboard.service nginx.service
sudo systemctl list-timers --all 'aurora-dev-live-pull-*.timer'
sudo systemctl list-timers --all 'aurora-*'
sudo journalctl -u 'aurora-dev-live-pull@*.service' --since '30 minutes ago' --no-pager
ls -1 /var/lib/aurora-cloud/dev-live-mirror/*.json
cat /data/aurora/internal/dev-live-mirror/last_success.json
```

Expected result:

- app returns the full dashboard document
- development banner is visible
- staged mirror timers are active and the legacy combined timer is inactive
- dashboard-product mirror lag is green in Operations
- normal production-path writer timers are disabled
- AURORACam, WXcam, Power, and Operations load from mirrored data

## Production Checks

On JASMIN:

```bash
sudo systemctl is-active aurora-dashboard.service nginx.service
sudo systemctl list-timers --all 'aurora-*'
sudo systemctl --failed --no-pager
sudo -u aurora git -C /opt/aurora-cloud-dashboard describe --tags --always --dirty
```

Expected result:

- app returns the full dashboard document
- no development banner
- checkout is clean
- HEAD is an approved `prod-*` tag
- writer timers are active after cutover
- no failed systemd units
- active streams show green freshness

## Rollback

Roll back the smallest changed surface. For a presentation-only release,
restore its saved web/API unit overrides and restart those serving processes.
Leave the writer checkout and observation files alone. For a reviewed shared
source release, use the captured source/configuration snapshot and explicitly
account for every writer that reads that checkout.

Release snapshots live under `/var/lib/aurora-release-snapshots/<UTC timestamp>`.
They contain protected configuration, so keep them off Git and the public docs
site. Check their manifest and checksums before restoring only the affected
files. Retain the failed release snapshot for diagnosis. Application rollback
never restores or deletes observation data.
