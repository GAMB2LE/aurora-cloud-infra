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

## Desired Layout

- Raw instrument data: `/project/aurora/raw`.
- Dashboard-ready products: `/data/aurora/products`.
- Application checkout: `/opt/aurora-cloud-dashboard`.
- Dashboard service user: `aurora`.
- Public endpoint: `https://data.gamb2le.co.uk`.
- Panel listens only on `127.0.0.1:5006`.
- `nginx` is the only public entrypoint for the dashboard.

## GWS Sync Position

JASMIN cloud tenant documentation says cloud VMs do not have native filesystem-level access to JASMIN storage, including Group Workspaces. GWS paths are available on transfer and analysis servers, not directly on cloud VMs. For that reason the primary supported sync mechanism should be `rsync` over SSH via a JASMIN transfer server such as `xfer-vm-01.jasmin.ac.uk`.

Optional `sshfs` can be added as a convenience mount at `/mnt/gws/gamb2le`, but it should not be the main production sync dependency. Automated sync should push:

- `/project/aurora/raw/` to `/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/`
- `/data/aurora/products/` to `/gws/ssde/j25b/gamb2le/data/output/aurora-cloud/products/`

## Required Before First Mutating Run

- Commit or otherwise preserve dirty production changes in the old dashboard repo.
- Commit or preserve dirty user systemd service changes, including HATPRO/radiometer timers.
- Decide how the target VM will authenticate to JASMIN transfer servers for GWS sync.
- Put any Tailscale auth key and SSH transfer key material in Ansible Vault, not plain Git.
- Confirm whether to keep existing `azimuth` desktop/VNC services or disable non-dashboard listeners.

## First Safe Commands

```bash
cd /gws/ssde/j25a/ncas_radar/jasmin_cloud_backup_20260409/aurora-cloud-infra
ansible-playbook playbooks/audit.yml
ansible-playbook playbooks/site.yml --check --diff
```
