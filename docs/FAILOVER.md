# Aurora Cloud Failover

This deployment supports a warm-standby droplet for `data.gamb2le.co.uk`.
The droplet normally serves the replicated dashboard at
`data-ocean.gamb2le.co.uk`; only after failover is initiated should it become
the `data.gamb2le.co.uk` host. Only one host should run writer timers at a time.

## Roles

- `aurora-cloud`: `aurora_failover_role: primary`,
  `aurora_domain: data.gamb2le.co.uk`
- `aurora-cloud-droplet`: `aurora_failover_role: standby`,
  `aurora_domain: data-ocean.gamb2le.co.uk`

The primary role enables source-sync, product-processing, quicklook, operations,
and GWS timers. The standby role installs the same code, units, nginx
configuration, and directory layout, but keeps those writer timers stopped.

The standby also runs `aurora-standby-pull.timer`, which pulls:

- `/project/aurora/raw/`
- `/data/aurora/products/`
- `/data/aurora/internal/`
- `/var/lib/aurora-cloud/`

from the primary host.

In standby, `data-ocean.gamb2le.co.uk` must point at `167.172.54.82` so the
droplet can serve HTTPS and certbot can maintain its own certificate. Do not
move `data.gamb2le.co.uk` to the droplet until promotion.

## Storage Preflight

A full standby currently needs to hold about `553G` of Aurora data:

- `/project/aurora/raw/`: about `95G`
- `/data/aurora/products/`: about `457G`
- `/data/aurora/internal/`: about `949M`

Use a 1TB-class data disk before enabling standby replication. If the droplet
has one large data disk rather than separate `/data` and `/project` filesystems,
mount it at `/data` and bind-mount a directory from that disk at `/project` so
the deployed paths still match the primary:

```fstab
/data/project /project none bind 0 0
```

Verify the final layout before running the playbook:

```bash
df -hT /data /project
```

## First Deployment

```bash
uv run ansible-galaxy collection install -r requirements.yml
uv run ansible-playbook playbooks/site.yml --limit aurora-cloud-droplet --check --diff
uv run ansible-playbook playbooks/site.yml --limit aurora-cloud-droplet
```

The droplet needs `/home/aurora/.ssh/id_rsa_jasmin_20200514` or the same key
provided through either `aurora_standby_replication_ssh_private_key_source` on
the Ansible controller or `aurora_standby_replication_ssh_private_key_content`
from Ansible Vault. It also needs that key accepted by `login.jasmin.ac.uk` and
the primary host. The replication script uses `sudo rsync` on the primary so it
can read the product and state trees.

The inventory pins the dashboard checkout to commit
`3295de15366480b7e14a5cad3cd30ac7c4bf66d2`, which includes the failover
endpoint checks and read-only standby catalog handling. `model-evaluation.py`
is tracked by that dashboard revision; the droplet still overlays it through
`aurora_dashboard_extra_files` during controller-based rollout so the running
host matches the primary exactly.

The standby can replicate from the primary without Tailscale, but promotion
requires the droplet to reach instrument source hosts. Provide `TAILSCALE_AUTHKEY`
when deploying the droplet or authenticate Tailscale manually before promotion.
The CL61 source sync also needs a source-authorized key before promotion; either
provide that key through vault/configuration or authorize the droplet-generated
`/home/aurora/.ssh/id_ed25519_celine.pub` on the CL61 source host.

## Parallel Standby Dashboard Checks

The standby dashboard should be usable at
`https://data-ocean.gamb2le.co.uk/app` while `data.gamb2le.co.uk` remains on
the primary. The expected first view is the `AURORA Data Viewer` with
`Instrument = Ceilometer`, because CL61 is the currently active Aurora data
stream.

Check the standby web path after every dashboard or replication change:

```bash
curl -L -sS -D - https://data-ocean.gamb2le.co.uk/app -o /tmp/data-ocean-app.html
sudo journalctl -u aurora-dashboard.service --since '10 minutes ago' --no-pager
sudo systemctl --failed --no-pager
sudo systemctl is-active aurora-dashboard.service nginx.service aurora-standby-pull.timer
```

A healthy Panel handler returns the full app document. A very small HTML body
with the browser title `Bokeh Application`, or a visually blank page, usually
means the Bokeh shell loaded but the Python app handler crashed before it could
publish the actual dashboard document. Confirm this from the dashboard journal;
do not diagnose it as a browser-only problem first.

On `2026-06-22`, the standby blank-page failure was traced to the WXcam SQLite
catalog. The app imports and builds WXcam panes during startup even when the
default visible instrument is `Ceilometer`, so a WXcam catalog exception can
blank the whole dashboard. The failing trace was:

```text
sqlite3.OperationalError: attempt to write a readonly database
```

from `wxcam_catalog.py` while opening
`/data/aurora/products/wxcam/wxcam_catalog.sqlite`.

The durable fix is in the dashboard repository:

- catalog reader helpers call `open_catalog(path, readonly=True)`
- reader helpers do not call `ensure_schema()`
- `open_catalog(..., readonly=True)` first tries SQLite `mode=ro`, then falls
  back to `mode=ro&immutable=1` for replicated standby catalogs where SQLite
  would otherwise try to create recovery or WAL metadata files

This file is included in pinned dashboard commit
`3295de15366480b7e14a5cad3cd30ac7c4bf66d2` and is also deployed through
`aurora_dashboard_extra_files` during controller-based rollout, so the patched
`wxcam_catalog.py` lands under `/opt/aurora-cloud-dashboard/` on both hosts.

The standby may log this non-fatal line because the product tree is replicated
read-only for the `aurora` service user:

```text
[perf] disabled: could not initialize /data/aurora/products/dashboard/dashboard_perf.jsonl: [Errno 13] Permission denied
```

That disables dashboard performance JSONL writes on the standby but should not
stop rendering.

During Aurora downtime, source-side services for Aurora subsystems such as
WXcam and ASFS may be failed or stale while CL61 remains active. Treat those as
source availability symptoms, not as evidence that the standby web service is
broken, provided the Ceilometer view renders and the dashboard journal has no
application traceback.

## Promotion

1. Point `data.gamb2le.co.uk` at `167.172.54.82`, or switch the load balancer.
2. Run one final pull if the primary is reachable:

   ```bash
   sudo systemctl start aurora-standby-pull.service
   ```

3. Promote the droplet by running the site play with primary role overrides:

   ```bash
   uv run ansible-playbook playbooks/site.yml \
     --limit aurora-cloud-droplet \
     -e aurora_domain=data.gamb2le.co.uk \
     -e aurora_failover_role=primary \
     -e aurora_certbot_enabled=true
   ```

   Moving DNS alone is not sufficient; this playbook run also switches nginx,
   Panel websocket origins, and alert URLs from the standby hostname to the
   main hostname.

4. Verify:

   ```bash
   sudo systemctl list-timers --all 'aurora-*'
   sudo systemctl status aurora-dashboard.service nginx
   curl --fail --silent --show-error --output /dev/null --write-out '%{http_code}\n' https://data.gamb2le.co.uk/app
   ```

## 2026-07-03 Cutover Notes

The droplet was promoted to live processing on `data-ocean.gamb2le.co.uk`
because the public `data.gamb2le.co.uk` A record still resolved to the JASMIN
VM. The zone is hosted on IONOS/1&1 nameservers (`ui-dns.*`), so moving the main
site still requires changing the `data.gamb2le.co.uk` A record to
`167.172.54.82` and then rerunning the promotion play with
`aurora_domain=data.gamb2le.co.uk`.

Before promotion, freeze the JASMIN primary by disabling/stopping Aurora writer
and GWS timers, then run the final `aurora-standby-pull.service`. Do not convert
the old JASMIN host to the Ansible `standby` role during this outage cutover.

The service account UID/GID must match the replicated data ownership. The
JASMIN primary uses `aurora` UID/GID `56781`, so the infra inventory now pins
the droplet to the same IDs. This avoids recursively rewriting the 500GB-class
replicated product/raw trees and keeps Zarr/appends writable after promotion.

Expected post-cutover red checks while Aurora sources are off:

- CL61 source sync can fail against the old `celine-edge-1` source until the
  CL61 move to `aurora-edge-1` is configured and authorized.
- WXcam, ASFS, radar, HATPRO, Vaisala, and power source streams can be stale
  until the Aurora source host is powered back on and reachable over Tailscale.
- GWS rsync should remain enabled; S3/object-store workflows are not part of
  this cutover.

## Failback

Do not simply restart writer timers on the original host. Treat the promoted
droplet as authoritative, sync data back to the old host, then deliberately move
`aurora_failover_role: primary` back to `aurora-cloud` and return the droplet to
`standby`.
