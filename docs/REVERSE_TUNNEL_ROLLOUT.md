# Reverse Tunnel Rollout

This checklist is the live operations procedure for enabling the ASS/APS
reverse SSH fallback through `data-ocean.gamb2le.co.uk`. Do not run the apply
steps without explicit approval for the current operations window.

## Stop points

The rollout has three separate approval gates:

1. Prepare data-ocean to accept reverse tunnels.
2. Start reverse-tunnel client services on ASS/APS Linux VMs.
3. Switch source-sync jobs from Tailscale to the tunnel endpoints after a soak
   period.

Gate 3 is intentionally later than Gates 1 and 2. SSH fallback can be verified
without changing active source-sync transport.

## Key material

Use two separate SSH keypairs:

| Purpose | Private key location | Public key installed in |
| --- | --- | --- |
| Edge clients open reverse tunnels to data-ocean | edge Ansible Vault, rendered to `/home/aurora/.ssh/id_ed25519_data_ocean_tunnel` | `edge_tunnel_server_authorized_keys` in this repo |
| data-ocean source-sync over forwarded ports | `/home/aurora/.ssh/id_ed25519_edge_source_sync` on data-ocean | `edge_source_sync_authorized_keys` in `GAMB2LE/aurora-edge-infra` |

Do not reuse the tunnel-client key for source sync.

Use `docs/examples/reverse_tunnel_vars.yml` as a non-secret template for the
cloud-side variables. Use `GAMB2LE/aurora-edge-infra/docs/examples/reverse_tunnel_vars.yml`
as the matching edge-side template.

## Preflight

Confirm current SSH access to data-ocean from the operator machine:

```bash
ssh root@data-ocean.gamb2le.co.uk hostname
```

Confirm the focused cloud playbook parses:

```bash
uvx --from ansible-core ansible-playbook playbooks/edge_tunnel_server.yml --syntax-check
```

Confirm the focused edge playbook parses in `GAMB2LE/aurora-edge-infra`:

```bash
uvx --from ansible-core ansible-playbook playbooks/reverse_tunnels.yml --syntax-check
```

## Gate 1: data-ocean server

Configure `edge_tunnel_server_authorized_keys` for `aurora-cloud-droplet`, then
run check mode:

```bash
ansible-playbook playbooks/edge_tunnel_server.yml --check --diff \
  -e edge_tunnel_server_enabled=true
```

Before applying with `edge_tunnel_server_manage_sshd_config=true`, verify
data-ocean includes sshd config fragments:

```bash
ssh root@data-ocean.gamb2le.co.uk "sudo sshd -T | grep -i '^allowtcpforwarding'"
ssh root@data-ocean.gamb2le.co.uk "grep -R '^Include /etc/ssh/sshd_config.d/\\*.conf' /etc/ssh/sshd_config"
```

Apply only after approval:

```bash
ansible-playbook playbooks/edge_tunnel_server.yml \
  -e edge_tunnel_server_enabled=true
```

If managing the optional sshd fragment, keep an existing admin SSH session open
while applying.

## Gate 2: edge tunnel clients

In `GAMB2LE/aurora-edge-infra`, configure:

- `edge_reverse_tunnel_private_key_content` or `edge_reverse_tunnel_private_key_source`
- `edge_source_sync_authorized_keys`

Run check mode:

```bash
ansible-playbook playbooks/reverse_tunnels.yml --check --diff \
  -e edge_managed_write_mode=true \
  -e edge_reverse_tunnels_enabled=true
```

Apply only after approval and after confirming ASS/APS collection and APS power
logging are healthy:

```bash
ansible-playbook playbooks/reverse_tunnels.yml \
  -e edge_managed_write_mode=true \
  -e edge_reverse_tunnels_enabled=true
```

## Verification

On data-ocean, verify listeners:

```bash
ss -ltn '( sport = :2201 or sport = :2202 )'
```

Verify SSH through the forwarded ports from data-ocean:

```bash
ssh -p 2201 aurora@127.0.0.1 hostname
ssh -p 2202 aurora@127.0.0.1 hostname
```

Verify from an operator machine through data-ocean:

```bash
ssh -J root@data-ocean.gamb2le.co.uk -p 2201 aurora@127.0.0.1 hostname
ssh -J root@data-ocean.gamb2le.co.uk -p 2202 aurora@127.0.0.1 hostname
```

## Gate 3: source-sync failover

Only after the tunnels have survived a soak period, switch source-sync transport
in this repo:

```bash
ansible-playbook playbooks/site.yml --check --diff \
  -e edge_source_sync_use_reverse_tunnels=true
```

Apply only after confirming the check-mode diff changes source-sync host/port
metadata and scripts as expected:

```bash
ansible-playbook playbooks/site.yml \
  -e edge_source_sync_use_reverse_tunnels=true
```

After applying, verify the expected source ports:

```bash
sudo systemctl start aurora-cl61-source-sync.service
sudo systemctl start aurora-power-source-sync.service
sudo journalctl -u aurora-cl61-source-sync.service -n 80 --no-pager
sudo journalctl -u aurora-power-source-sync.service -n 80 --no-pager
```

Do not disable Tailscale while validating this path. The reverse tunnels are a
fallback path first, not an immediate replacement for normal operations.
