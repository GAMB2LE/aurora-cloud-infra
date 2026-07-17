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

Development owns:

- the public development dashboard
- `aurora-dev-live-pull.timer`
- a mirror-lag success stamp at
  `/data/aurora/internal/dev-live-mirror/last_success.json`
- experimental paths only:
  - `/project/aurora/dev-raw`
  - `/data/aurora/dev-products`

The development mirror pulls production raw, products, internal state, and
required service state about every five minutes. It uses rsync locking,
`--partial`, `--delay-updates`, and `--delete-delay` so incomplete transfers do
not replace complete products.

Development also runs `aurora-ecmwf-provider-shadow.timer`. This performs a
read-only comparison of the latest mirrored deterministic ECMWF GRIB with the
legacy and Earthkit decoders. It writes only
`/data/aurora/dev-products/power/ecmwf_provider_shadow.json`, appends a shadow
history, and writes a daily promotion-gate report. The gate requires seven days
and 50 clean comparisons before it can be reviewed; it never changes the
configured provider. `aurora-dashboard-health-probe.timer` also compares public
development and production response times every five minutes and records mirror
age. It does not run a forecast writer or modify mirrored production products.
Production remains on `AURORA_ECMWF_PROVIDER=legacy` until the parity and
resource gates pass.

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
dashboard service set, including source sync, GWS, nginx, and development
mirror units. Keep writers disabled while preparing a production host:

```bash
uv run ansible-playbook playbooks/dashboard_runtime_release.yml --limit <host> --check --diff -e aurora_writer_timers_enabled=false
uv run ansible-playbook playbooks/dashboard_runtime_release.yml --limit <host> -e aurora_writer_timers_enabled=false
```

The runtime playbook assumes the host baseline, storage, and network roles have
already been provisioned. Run `playbooks/site.yml` separately for those host
baseline changes; its check mode can report package/service ordering failures
when a package is absent and would only be installed during the same run.

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
sudo systemctl is-active aurora-dashboard.service nginx.service aurora-dev-live-pull.timer
sudo systemctl list-timers --all 'aurora-*'
sudo journalctl -u aurora-dev-live-pull.service --since '30 minutes ago' --no-pager
cat /data/aurora/internal/dev-live-mirror/last_success.json
```

Expected result:

- app returns the full dashboard document
- development banner is visible
- mirror lag is green in Operations
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

Only roll back data products from a separately preserved product backup, and
only after confirming the product rollback is needed.
