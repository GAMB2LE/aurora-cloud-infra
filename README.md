# Aurora Cloud Infrastructure

Ansible configuration for rebuilding the Aurora cloud dashboard host on the existing JASMIN Cloud VM.

## Current Contract

- Public hostname: `data.gamb2le.co.uk`.
- Raw data: `/project/aurora/raw`.
- Dashboard products: `/data/aurora/products`.
- Dashboard app: `/opt/aurora-cloud-dashboard`.
- Public access: `nginx` on `80/443`.
- Private Panel backend: `127.0.0.1:5006` only.
- GWS backup/sync: rsync via a JASMIN transfer host to `/gws/ssde/j25b/gamb2le`.

## Safe First Steps

```bash
ansible-galaxy collection install -r requirements.yml
ansible-playbook playbooks/audit.yml
ansible-playbook playbooks/site.yml --check --diff
```

Do not run `playbooks/site.yml` without `--check` until the old production Git changes have been preserved and transfer/Tailscale secrets have been put in Ansible Vault.
