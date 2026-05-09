# Aurora Cloud Rebuild Plan

## Audited Target

- Host: `aurora-cloud-workstation-ssh`
- Public IP: `130.246.212.116`
- Public DNS: `data.gamb2le.co.uk` already resolves to this IP.
- OS: Ubuntu 22.04.5 LTS.
- Compute: 16 vCPU, 58 GiB RAM.
- Root: 400G ext4, lightly used.
- `/data`: 999G ext4, currently mounted and bind-mounted to `/home/azimuth`.
- `/project`: 4T Ceph mount, currently empty from the VM view.
- Current public web state: no `nginx`, no `certbot`, no listeners on `80`, `443`, or `5006`.
- UFW package exists but firewall is inactive.
- Existing listeners to preserve by request: VNC `5901`, node exporter `9100`, rpcbind `111`, and cups. CUPS currently listens on localhost only.

## Desired Layout

- Raw instrument data: `/project/aurora/raw`.
- Dashboard-ready products: `/data/aurora/products`.
- Application checkout: `/opt/aurora-cloud-dashboard`.
- Dashboard service user: `aurora`.
- Public endpoint: `https://data.gamb2le.co.uk`.
- Panel listens only on `127.0.0.1:5006`.
- `nginx` is the only public entrypoint for the dashboard.
- Existing VNC, node exporter, rpcbind, and cups services are left installed and running.

## Current Remote Audit: 2026-05-07

- `azimuth@130.246.212.116` is already in the Ansible layout:
  `/opt/aurora-cloud-dashboard`, `/project/aurora/raw`, and
  `/data/aurora/products`.
- `aurora-dashboard.service`, `nginx`, `tailscaled`, `fail2ban`, and `ufw` are
  active.
- Processing product directories exist but are empty. The dashboard serves a
  Panel shell, but the app logs show missing Zarr data because no fresh source
  data has been pulled yet.
- `100.117.101.84` is visible from `azimuth` over Tailscale as
  `celine-edge-1`, but SSH as `aurora` currently fails with
  `Permission denied (publickey,password)`.

## GWS Sync Position

JASMIN cloud tenant documentation says cloud VMs do not have native
filesystem-level access to JASMIN storage, including Group Workspaces. GWS
paths are available on transfer and analysis servers, not directly on cloud
VMs. For that reason the primary supported sync mechanism should be `rsync`
over SSH via a JASMIN transfer server. The deployed failover order is:

1. `xfer-vm-03.jasmin.ac.uk`
2. `xfer-vm-01.jasmin.ac.uk`
3. `xfer-vm-02.jasmin.ac.uk`

Optional `sshfs` can be added as a convenience mount at `/mnt/gws/gamb2le`, but it should not be the main production sync dependency. Automated sync should push:

- `/project/aurora/raw/` to `/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/`
- `/data/aurora/products/` to `/gws/ssde/j25b/gamb2le/data/output/aurora-cloud/products/`
- `/data/aurora/internal/mirror_manifests/` to `/gws/ssde/j25b/gamb2le/data/internal/aurora-cloud/manifests/`

## Required Before First Mutating Run

- Commit or otherwise preserve dirty production changes in the old dashboard repo.
- Commit or preserve dirty user systemd service changes, including HATPRO/radiometer timers.
- Add the generated public key from
  `/home/aurora/.ssh/id_ed25519_jasmin_gws.pub` to the JASMIN account used for
  `rrniii`.
- Put any Tailscale auth key and SSH transfer key material in Ansible Vault, not plain Git.
- The user provided a Tailscale key in the chat, but it is intentionally not stored in this repository.

## Fresh Source Plan

- Pull CL61 files from `aurora@100.117.101.84:/home/aurora/data/cl61` into
  `/project/aurora/raw/cl61`.
- Install and enable the Ansible-managed source sync timer now that SSH from the
  `aurora` service user on `azimuth` is authorized.
- Pull the full current source history. The source sync maintains
  `/var/lib/aurora-cloud/cl61-sync.last`; the first run initializes it to `0`
  when the state file is absent.
- Keep the legacy source-side `cl61sync.timer` disabled. It pushes to the old
  `/mnt/data/cl61` destination and prunes source files after verification.
- The CL61 append service bootstraps
  `/data/aurora/products/cl61/gamb2le_depolarisation_lidar_ceilometer_aurora.zarr`
  from recent raw files when the Zarr store is absent.
- CL61 append, latest quicklook, and daily quicklook timers are enabled.
- Pull cloud radar files from
  `aurora@100.124.55.22:/home/aurora/data/rpgfmcw94` into
  `/project/aurora/raw/rpgfmcw94`, preserving the recursive
  `Yyyyy/Mmm/Ddd/` source tree.
- Pull the full current source history. The source sync maintains
  `/var/lib/aurora-cloud/radar-sync.last`; the first run initializes it to `0`
  when the state file is absent.
- Radar source sync uses Tailscale SSH over the tailnet IP with private-key
  authentication disabled.
- Radar append and radar quicklook timers are enabled now that the fresh raw
  source is configured.
  HATPRO timers remain disabled until its fresh raw source is configured.
- The dashboard code should be deployed from a GitHub commit that includes
  environment-driven quicklook paths and missing-Zarr tolerance.
- Mirror the full wxcam raw tree locally. Wxcam science and housekeeping
  products still use the HDR subsets, but raw retention checks should use the
  full local mirror plus the GWS mirror.

## First Safe Commands

```bash
cd /gws/ssde/j25a/ncas_radar/jasmin_cloud_backup_20260409/aurora-cloud-infra
uv run ansible-galaxy collection install -r requirements.yml
uv run ansible-playbook playbooks/audit.yml
uv run ansible-playbook playbooks/site.yml --check --diff
```
