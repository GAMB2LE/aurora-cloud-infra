# Production and Development Sites

This is the current operating model for the Aurora dashboard.

## Endpoints

| Site | URL | Host | Role |
| --- | --- | --- | --- |
| Production | `https://data.gamb2le.co.uk/app` | JASMIN `aurora-cloud` at `130.246.212.116` | Stable public site and authoritative writer |
| Development | `https://data-ocean.gamb2le.co.uk/app` | DigitalOcean `aurora-cloud-droplet` | Public staging site with live mirrored production data |

Production should optimize for stability. Development can change faster, but it
must clearly show the banner `Development site - live mirrored data`.

## Host Roles

Production uses:

```yaml
aurora_site_env: production
aurora_domain: data.gamb2le.co.uk
aurora_failover_role: primary
aurora_writer_timers_enabled: true
```

Development uses:

```yaml
aurora_site_env: development
aurora_domain: data-ocean.gamb2le.co.uk
aurora_failover_role: standby
aurora_writer_timers_enabled: false
aurora_standby_replication_timer_enabled: true
```

`aurora_site_env` is now the source of truth for whether normal raw/product
writer timers run. The older `aurora_failover_role` is retained for compatibility
with existing playbooks and templates.

## Live Data Flow

Production owns:

- `/project/aurora/raw`
- `/data/aurora/products`
- source-sync timers
- UAS Menapia MQTT source-sync timer
- Menapia S3 flight-data source-sync timer, once separately commissioned
- Menapia flight JSON/PNG and UAS science-quicklook producer every 30 minutes
- append/build timers
- quicklook timers
- Operations monitor and alert timers
- GWS archive sync and verification timers
- object-store writers, sharded inventory, exact repair, archive health, and
  the retention coordinator

Development owns:

- the public development dashboard
- the isolated AURORA Iceland model-evaluation science workspace and
  evaluator-owned replay/daily units on data-ocean; these are not normal
  dashboard writer timers
- independent `aurora-dev-live-pull-<stage>.timer` units for each raw or
  product family
- `product-menapia` and `product-quicklooks` mirrors of production-owned
  Menapia JSON/PNG products; development does not rebuild them
- the shared `aurora-dev-live-pull@.service` template used by those timers
- a mirror-lag success stamp at
  `/data/aurora/internal/dev-live-mirror/last_success.json`
- experimental paths only:
  - `/project/aurora/dev-raw`
  - `/data/aurora/dev-products`

The model-evaluation exception is deliberate: `aurora-model-evaluation` owns
its executable units, science environment, and campaign products. The
`dashboard_services` role neither installs nor starts them. Approved compact
campaign artifacts can be published to the production dashboard and archive
roots without giving data-ocean ownership of normal instrument writers.

Most development stages pull production raw, products, internal state, and
required service state about every five minutes. AURORACam raw and product
stages run every two minutes. Every stage has its own lock and status JSON, so
a large camera or radar scan cannot block Power or dashboard summaries. The
legacy combined `aurora-dev-live-pull.timer` is installed for compatibility but
disabled while staged timers are active.

The mirror uses `--partial`, `--delay-updates`, and `--delete-delay` so an
incomplete transfer does not replace a complete product. Only a successful
`product-dashboard` stage updates the public
`/data/aurora/internal/dev-live-mirror/last_success.json` stamp used for common
dashboard freshness.

That mirror is for service availability and development testing. It is not an
independent long-term archive and never counts as GWS/object-store parity or as
permission to prune ASS. When production ownership moves during a deliberate
failover, archive-writer and retention ownership must move as one explicitly
reviewed unit; two hosts must never coordinate deletion concurrently.

Development also runs `aurora-ecmwf-provider-shadow.timer`. This performs a
read-only comparison of the latest mirrored deterministic ECMWF GRIB with the
legacy and Earthkit decoders. It writes only
`/data/aurora/dev-products/power/ecmwf_provider_shadow.json`, appends a shadow
history, and writes a daily promotion-gate report. The gate requires seven days
and 50 clean comparisons before it can be reviewed; it never changes the
configured provider. `aurora-dashboard-health-probe.timer` also compares public
development and production response times every five minutes and records mirror
age. A development-versus-production latency delta is an observation only: it
does not fail the availability probe when both endpoints are healthy.

Development may run advisory forecast writers only in
`/data/aurora/dev-products/power`; it never modifies the mirrored production
forecast products. Production runs the same advisory jobs in
`/data/aurora/products/power`. The operating-scenario service reads the UAS
MQTT mirror so it can learn effective-tier load evidence. Neither environment
issues PDU commands.

When issue-driven Power orchestration is enabled on development, one successful
archive-eligible deterministic issue captures an immutable issue snapshot and
starts the ENS and 240-hour planning inputs in that order. Only after both
inputs have been copied into the same staging tree do the deterministic,
planning, and ENS snapshots pass explicit generation-age, source-cycle-age,
90-hour coverage, and cycle-compatibility checks. Scenario, display, and
checksum work then reads only those staged copies. The complete manifest binds
the three forecast identities, upstream source-manifest digests, and artifact
hashes into one `sourceManifestDigest` and
also covers display-energy, the generation-local recommendation archive, and
CL61 diagnostic products. The scenario generator seeds recommendation history
and adaptive load-model state from the checksum-verified previous complete
bundle (or the legacy files for the first generation) only into staging;
neither an unpublished run nor a failed bundle mutates the legacy development
learner state or recommendation archive. Completed issue and generation trees
have their write bits removed before a pointer can expose them. A failed stage
retains the prior generation and records a fail-closed status with the first
detailed failure preserved within a distinct attempt ID; the next deterministic
or cached attempt clears that failure before doing work, so stale errors cannot
hide a later run's cause.

The full-cycle and cached deterministic writers share one exclusive file lock.
The dev full-cycle runner retains that lock across deterministic issue capture,
ENS generation, planning generation, and complete staged publication. This
prevents the three-hour timer or a cached re-anchor from replacing the issue or
mutable source products between stages. A cached run that finds the full runner
busy is deferred as a false systemd condition and cannot trigger a publisher
against stale output. Otherwise its generation and complete staged publication
occur while it still holds the lock. Manually invoked ENS, planning, and
full-publication services also take the same lock, while the cached path calls
the publisher directly under its existing lock and never recursively acquires
it.
Cached re-anchors are labelled `independentCycle=false` and never advance the
`latest-independent` pointer used by candidate evidence. Cached rows remain in
the forecast archive with
`ForecastVerificationEligible=false`, so operational re-anchor behavior is
auditable without entering paired skill or adaptive-learning evidence. The
independent planning, ENS, scenario, candidate, and display timers are disabled
on development while this chain is active.

Public activation is deliberately two phase. The initial machinery release
keeps `aurora_power_forecast_publication_active: false`, leaves
`forecast-bundle/active` absent, and continues serving the established dev
display without adding a candidate warning to the legacy v10 payload. Once
`current/generation.json` is a real complete advisory-only bundle, set that
inventory switch to `true` and re-run the focused release. Only then can a
missing ready marker report `activation_pending`. Ansible validates the
manifest from one resolved generation and links `active` directly to that same
immutable generation; no legacy directory or moving `current` symlink is ever
presented as the validated activation target. Subsequent successful
publications advance `current` and
`active` to the same generation. `current` is internal publisher state; every
activated consumer resolves its products through the single validated `active`
pointer so readers cannot mix generations during pointer updates.

The v12 evaluator receives the deterministic and ENS snapshots from one
checksum-verified `latest-independent` generation. Its launcher rejects cached,
incomplete, mismatched, or modified bundles before invoking the candidate.
Candidate forcing comes from the site-level `ECMWFSolarIrradiance` embedded in
the immutable deterministic artifact; it never reopens or retains the roughly
50 MB global-grid GRIB named by historical provenance.
The launcher atomically records preflight start/failure in the candidate status
and append-only evaluation history, so an identity, digest, or anchor rejection
is visible to operations and iOS rather than only in the systemd journal.
It remains constrained by `MemoryMax=1.5G`, defers while the separate AURORA
model-evaluation service is active, writes only its candidate tree, and is not a
dependency of the operational Power bundle. CL61 output remains diagnostic
shadow intent with `cl61ActuationEnabled=false`; intent and current status live
only inside each validated bundle. The separate CL61 history remains an
append-only diagnostic evidence stream and cannot actuate or update a PDU.
The additive development mobile endpoint reads the status from the validated
`current` bundle even while public forecast-bundle activation remains off; the
standalone scenario writer keeps a separate mutable output path and can never
write into that immutable bundle.
Production remains on `AURORA_ECMWF_PROVIDER=legacy` until the parity and
resource gates pass.

Forecast and scenario services use semantic publication signatures. A run with
unchanged SOC/load anchors, mode, ECMWF cycle, solar calibration, battery
parameters, and model version updates service state without rewriting the
public Zarr or adding a duplicate verification issue.

## Development-only display performance work

The development host may run bounded presentation experiments that do not
change raw data, product Zarrs, source synchronization, or writer ownership.

`aurora-dashboard-display-manifest.timer` inventories prewarmed Plotly JSON,
quicklooks, WXcam thumbnails, and daily videos every five minutes. The manifest
is an atomic, bounded input for a future CDN or object-store publishing job; it
does not publish raw data and does not move any Zarr store.

Development expires unused Panel documents after one minute so backgrounded
phone sessions stop retaining full server-side documents promptly. Production
uses two minutes. Both hosts check every 15 seconds and retain a 24-hour
session-token lifetime.

## Release Policy

Branches and tags:

- `main`: staging/development branch for data-ocean.
- `prod-YYYYMMDD.N`: annotated production release tags.

Promotion sequence:

1. Deploy the candidate to data-ocean.
2. Run smoke tests on `https://data-ocean.gamb2le.co.uk/app`.
3. Confirm data-ocean shows the development banner and live mirror lag.
4. Create an annotated `prod-*` tag on the validated `main` commit.
5. Deploy exactly that tag to JASMIN.
6. Smoke-test `https://data.gamb2le.co.uk/app`.

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

UI rollback should not delete or roll back data products:

```bash
sudo -u aurora git -C /opt/aurora-cloud-dashboard fetch --tags origin
sudo -u aurora git -C /opt/aurora-cloud-dashboard checkout <previous-prod-tag>
sudo systemctl restart aurora-dashboard.service
```

Before every dashboard, mobile API, runtime, or security release, the release
playbooks create a root-only snapshot under
`/var/lib/aurora-release-snapshots/<UTC timestamp>/`.  It contains the previous
source identity, a dirty-worktree patch when present, service status, a list of
untracked files, and a root-only archive of the dashboard environment, mobile
API token, relevant systemd units, Nginx configuration, and alert-client
configuration.  It does not enter Git or the public documentation portal.

To restore a configuration as part of an approved rollback, first inspect the
snapshot manifest and file list, then restore its archive from `/` and reload
only the affected units:

```bash
sudo tar -xzf /var/lib/aurora-release-snapshots/<timestamp>/configuration.tar.gz -C /
sudo systemctl daemon-reload
sudo systemctl restart aurora-dashboard.service aurora-mobile-api.service
```

Confirm the recorded checksums before restart and retain the failed release
snapshot for diagnosis.  Do not restore product data as part of a UI rollback.

Only roll back data products from a separately preserved product backup, and
only after confirming the product rollback is needed.
