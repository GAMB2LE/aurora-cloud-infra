# Aurora Cloud Infrastructure

Ansible configuration for rebuilding the Aurora cloud dashboard host on the existing JASMIN Cloud VM.

## Current Contract

- Public hostname: `data.gamb2le.co.uk`.
- Raw data: `/project/aurora/raw`.
- Dashboard products: `/data/aurora/products`.
- Dashboard app: `/opt/aurora-cloud-dashboard`.
- Public access: `nginx` on `80/443`.
- Private Panel backend: `127.0.0.1:5006` only.
- Fresh CL61 raw source: `aurora@100.117.101.84:/home/aurora/data/cl61` pulled into `/project/aurora/raw/cl61`.
- GWS backup/sync: rsync via a JASMIN transfer host to `/gws/ssde/j25b/gamb2le`.

## Safe First Steps

```bash
uv run ansible-galaxy collection install -r requirements.yml
uv run ansible-playbook playbooks/audit.yml
uv run ansible-playbook playbooks/site.yml --check --diff
```

Do not run `playbooks/site.yml` without `--check` until the old production Git changes have been preserved and transfer/Tailscale secrets have been put in Ansible Vault.

## CL61 Fresh Start

The CL61 source sync intentionally does not migrate historical data. The first
successful `aurora-cl61-source-sync.service` run creates
`/var/lib/aurora-cloud/cl61-sync.last` with the current epoch and exits. Later
runs pull only source files newer than that marker.

Before enabling this live, confirm SSH from the target works:

```bash
sudo -u aurora ssh -i /home/aurora/.ssh/id_ed25519_celine aurora@100.117.101.84 true
```

The current audit found Tailscale reachability to `100.117.101.84`
(`celine-edge-1`) and passwordless SSH now works from the `aurora` service user
on `azimuth` using `/home/aurora/.ssh/id_ed25519_celine`. The source contains
fresh files in `/home/aurora/data/cl61`.

The legacy source-side `cl61sync.timer` on `celine-edge-1` pushes to the old
`aurora-cloud:/mnt/data/cl61` location and prunes local files older than 21 days
after a successful verification. Leave that timer disabled for this fresh-start
pull model.

## Secrets

Do not commit secrets. For a first Tailscale registration, pass the auth key from the environment:

```bash
export TAILSCALE_AUTHKEY=...
uv run ansible-playbook playbooks/site.yml --check --diff
```

For unattended GWS sync, create or install a dedicated private key at `/home/aurora/.ssh/id_rsa_jasmin` and authorize it for `rrniii` on the relevant JASMIN transfer service. A forwarded SSH agent from an interactive admin session is not enough for systemd timers.

For CL61 source sync, either let Ansible generate
`/home/aurora/.ssh/id_ed25519_celine` on the target and add its `.pub` file to
the source, or store an existing private key in Ansible Vault as
`cl61_source_ssh_private_key_content`.
