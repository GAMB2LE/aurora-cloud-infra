# Aurora Cloud Infrastructure

This repo contains the Ansible configuration used to rebuild and operate the
Aurora cloud dashboard host on the existing JASMIN Cloud VM.

## What this repo covers

- source sync timers and services for the instrument hosts
- deployment of the dashboard systemd services
- observe-only operations health-sentinel outputs
- GWS transfer and mirror-verification jobs
- WXcam FISH HDR mirroring policy and downstream processing support
- host-level operational configuration for the Aurora stack

## Current deployment contract

- Public hostname: `data.gamb2le.co.uk`
- Raw mirror root: `/project/aurora/raw`
- Product root: `/data/aurora/products`
- Dashboard app checkout: `/opt/aurora-cloud-dashboard`
- Public frontend: `nginx` on `80/443`
- Private Panel backend: `127.0.0.1:5006`
- Dashboard sessions use a `15 s` websocket keepalive, `1 h` unused-session
  lifetime, and `24 h` session-token expiration to improve recovery after
  short mobile backgrounding events.
- `/wxcam-media` is served from `/data/aurora/products/wxcam` so WXcam MP4s
  stream over normal HTTP with byte-range support.
- Operations health reports are written under
  `/data/aurora/products/ops_monitor/health` by the observe-only collector.

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

- CL61
- Cloud Radar
- Vaisala MET
- ASFS Logger
- ASFS Fast Sonic
- Power
- WXcam

WXcam is mirrored locally as FISH HDR JPG and MP4 files only; PANO and
AUTO/LONG/SHORT assets remain on the source host.

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

Docs are published through the central `GAMB2LE/mkdocs-portal` build only. This
repo keeps `trigger-docs.yml` for portal dispatch and no longer deploys a
standalone repo-local Pages site.

## Key docs in this site

- **Rebuild Plan** for host rebuild and recovery notes
- **Source Syncs** for per-instrument sync behavior and deployment details

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
