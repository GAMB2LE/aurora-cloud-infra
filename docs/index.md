# Aurora Cloud Infrastructure

This repo contains the Ansible configuration used to build and operate the
Aurora cloud dashboard hosts on JASMIN Cloud and the DigitalOcean droplet.

## What this repo covers

- source sync timers and services for the instrument hosts
- deployment of the dashboard systemd services
- observe-only operations health-sentinel outputs
- GWS transfer and mirror-verification jobs
- optional edge reverse-tunnel server configuration for ASS/APS access through
  data-ocean
- WXcam HDR mirroring policy and downstream processing support
- host-level operational configuration for the Aurora stack

## Current deployment contract

- Production public hostname: `data.gamb2le.co.uk` on JASMIN
- Development public hostname: `data-ocean.gamb2le.co.uk` on DigitalOcean
- Production is the authoritative live writer.
- Development stays live by running `aurora-dev-live-pull.timer` and mirroring
  production about every five minutes.
- Raw mirror root: `/project/aurora/raw`
- Product root: `/data/aurora/products`
- Development experiment roots: `/project/aurora/dev-raw` and
  `/data/aurora/dev-products`
- Dashboard app checkout: `/opt/aurora-cloud-dashboard`
- Public frontend: `nginx` on `80/443`
- Private Panel backend: `127.0.0.1:5006`
- Dashboard sessions use a `15 s` websocket keepalive, `1 h` unused-session
  lifetime, and `24 h` session-token expiration to improve recovery after
  short mobile backgrounding events.
- `/wxcam-media` is served from `/data/aurora/products/wxcam` so WXcam MP4s
  stream over normal HTTP with byte-range support.
- `/auroracam-media` is served from `/project/aurora/raw/auroracam` so MX4
  JPEGs load over normal HTTP image requests.
- Operations health reports are written under
  `/data/aurora/products/ops_monitor/health` by the observe-only collector.
- Operations email alerts are evaluated by `aurora-ops-monitor-alerts.timer`
  from `/project/aurora/raw/ops_monitor/latest.json`. The alert service uses
  `mailx` backed by `msmtp`/`msmtp-mta` or another sendmail-compatible relay and
  keeps alert state under `/data/aurora/products/ops_monitor/alerts`.
  `mailx` is only the script-facing command line interface; `msmtp` is the
  lightweight outbound SMTP delivery layer.
  Configure `ops_alert_smtp_host` and related variables to install the Aurora
  service user's `.msmtprc`; without a relay, alert evaluation still works but
  real email delivery cannot complete. The dashboard alert script treats the
  msmtp-backed `mailx` path as ready only after an msmtp config is present.

## Storage model

The deployed host separates raw mirrored inputs from derived products:

- `/project/aurora` is the raw landing and mirror area on the shared Ceph
  filesystem
- `/data/aurora` is the local product, work, and output area for Zarrs,
  quicklooks, catalogs, videos, and logs

This lets us rebuild products without touching the source mirror and keeps the
dashboard-serving artifacts on local disk.

## Source streams

The deployed syncs currently cover:

- CL61, pulled from the ASS Linux data path over Tailscale SSH
- Cloud Radar
- HATPRO
- Vaisala MET
- ASFS Logger
- ASFS Fast Sonic
- Power
- WXcam
- MX4 camera FTP ingest on the ASS Linux data volume
- AURORACam cloud mirror and metadata Zarr

WXcam is mirrored locally as FISH HDR and PANO HDR JPG/MP4 files only;
AUTO/LONG/SHORT assets remain on the source host.

The MX4 camera ingest is source-side FTP on `ass-proxmox-linux`: four MOBOTIX
M24 cameras upload one QXGA JPEG per minute under
`/home/aurora/data/mx4/<camera>/YYYY-MM-DD/` using
`<camera>_YYYY-MM-DD_HH-MM.jpg` filenames. A separate AURORACam source sync
mirrors that tree to `/project/aurora/raw/auroracam` and rebuilds
`/data/aurora/products/auroracam/auroracam.zarr`. It is intentionally separate
from the CL61 SSH/SFTP path.

ASFS science and fast-sonic syncs use the current CRD source directory
(`/home/aurora/data/asfs/raw/crd`) and the May 2 onward retained data window.

## GWS transfer model

The current backup and archive design is push-based from the Aurora VM to:

`/gws/ssde/j25b/gamb2le`

It uses systemd timers plus rsync-over-SSH failover across:

1. `xfer-vm-03.jasmin.ac.uk`
2. `xfer-vm-01.jasmin.ac.uk`
3. `xfer-vm-02.jasmin.ac.uk`

Verification manifests are generated for source, local raw, and GWS copies so
upstream pruning decisions can be made against evidence rather than trust.
Product sync is split into core products and WXcam products so the large WXcam
media tree cannot delay the smaller product artifacts.

## Production and development state

`aurora-cloud` on JASMIN is the production endpoint for
`data.gamb2le.co.uk`. It is the only host that should run normal source-sync,
append, quicklook, Operations, alert, and GWS writer timers after cutover.

`aurora-cloud-droplet` on DigitalOcean is the public development endpoint for
`data-ocean.gamb2le.co.uk`. It should not run normal production-path writer
timers. It mirrors production with `aurora-dev-live-pull.timer`, displays the
development banner, and reports mirror lag in the Operations Dashboard.

Unattended Tailscale SSH from the droplet to `ass-proxmox-linux` and
`aps-proxmox-linux` must use an `accept` policy for Linux user `aurora`, not an
interactive `check` policy. CL61 source sync now uses the ASS Linux data path
at `100.124.55.22:/home/aurora/data/cl61`.

The active droplet data disk is 1TB-class and is shared by `/data` and
`/project`. The last resource audit still showed the smaller `4 vCPU / 7.8 GiB`
compute size with no swap; resize to `8 vCPU / 16 GiB` or add swap for safer
live-processing headroom.

Docs are published through the central `GAMB2LE/mkdocs-portal` build only. This
repo keeps `trigger-docs.yml` for portal dispatch and no longer deploys a
standalone repo-local Pages site.

## Key docs in this site

- **Rebuild Plan** for host rebuild and recovery notes
- **Production and Development** for host roles, release policy, mirror checks,
  and rollback rules
- **Failover** for emergency promotion history and troubleshooting
- **Reverse Tunnels** for cloud-side source access and staged rollout checks
- **Data Locations** for source, local raw, local product, GWS archive, and
  active Zarr paths
- **Source Syncs** for per-instrument sync behavior and deployment details
- **MX4 Camera FTP Ingest** for the MOBOTIX camera FTP endpoint and camera-side
  exposure/upload settings
- **AURORACam Source Sync** for the cloud mirror, metadata Zarr, and dashboard
  media route

## Safe working pattern

Typical first checks:

```bash
uv run ansible-galaxy collection install -r requirements.yml
uv run ansible-playbook playbooks/audit.yml
uv run ansible-playbook playbooks/site.yml --check --diff
```

Avoid applying `playbooks/site.yml` directly until secrets, auth, and any
production drift have been checked carefully.

## Source repository

- GitHub: <https://github.com/GAMB2LE/aurora-cloud-infra>
