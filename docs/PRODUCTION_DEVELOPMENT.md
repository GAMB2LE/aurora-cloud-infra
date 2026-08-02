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

The development 240-hour planning forecast is advisory. It attempts a bounded
ECMWF refresh and then a bounded cached re-anchor. If both fail, the service
retains the last published plan and exits cleanly with an explicit journal
message; this must not be treated as an acquisition failure.
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
