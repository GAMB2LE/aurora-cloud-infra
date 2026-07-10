# Edge Reverse Tunnels

This is the Tailscale-independent SSH fallback for reaching the ASS and APS
Linux VMs through `data-ocean.gamb2le.co.uk` when the Starlink or 5G WAN path
is reachable but direct inbound SSH is not.

The active design is:

| Edge host | data-ocean bind | Edge target |
| --- | --- | --- |
| `ass-proxmox-linux` | `127.0.0.1:2201` | `127.0.0.1:22` |
| `aps-proxmox-linux` | `127.0.0.1:2202` | `127.0.0.1:22` |

The reverse tunnel clients live in `GAMB2LE/aurora-edge-infra`. The data-ocean
server-side account and SSH restrictions live in this repo.

Source-sync failover support is also present in this repo, but it is disabled by
default. The live source-sync hosts and ports remain the Tailscale values until
`edge_source_sync_use_reverse_tunnels=true` is set.

## Server role

The `edge_tunnel_server` role is wired into `playbooks/site.yml` but disabled by
default:

```yaml
edge_tunnel_server_enabled: false
edge_tunnel_server_allowed_inventory_hosts:
  - aurora-cloud-droplet
```

When enabled on `aurora-cloud-droplet`, it creates a locked
`aurora-tunnel` system account and installs authorized keys restricted to remote
port forwarding for `127.0.0.1:2201` and `127.0.0.1:2202`.

The optional sshd `Match User` fragment is also disabled by default:

```yaml
edge_tunnel_server_manage_sshd_config: false
```

Only enable that after confirming `/etc/ssh/sshd_config` includes
`/etc/ssh/sshd_config.d/*.conf`.

## Client role

In `GAMB2LE/aurora-edge-infra`, the `edge_reverse_tunnel` role is attached to
`linux_vms` and disabled by default:

```yaml
edge_managed_write_mode: false
edge_reverse_tunnels_enabled: false
```

`ass-proxmox-linux` maps to `127.0.0.1:2201` and `aps-proxmox-linux` maps to
`127.0.0.1:2202`.

## Source-sync tunnel transport

The source-sync scripts can be rendered for either Tailscale SSH or the reverse
tunnel endpoints:

```yaml
edge_source_sync_use_reverse_tunnels: false
```

With the default `false`, source-sync jobs continue to use the Tailscale IPs on
port `22` and Tailscale SSH authentication.

With `true`, ASS-backed streams use `127.0.0.1:2201`, APS-backed streams use
`127.0.0.1:2202`, and the scripts use normal SSH key authentication through the
forwarded edge SSH daemon. That requires a data-ocean private key:

```yaml
edge_source_sync_ssh_key_path: /home/aurora/.ssh/id_ed25519_edge_source_sync
edge_source_sync_ssh_private_key_content: ""
edge_source_sync_ssh_private_key_source: ""
```

The matching public key must be authorized for the `aurora` user on
`ass-proxmox-linux` and `aps-proxmox-linux` before switching source-sync jobs to
the tunnel transport. In `GAMB2LE/aurora-edge-infra`, put that public key in
`edge_source_sync_authorized_keys`.

## SSH access

After both sides are enabled and the tunnel services are running, connect
through data-ocean. Replace `root` with whichever admin account you normally use
to reach the droplet:

```bash
ssh -J root@data-ocean.gamb2le.co.uk -p 2201 aurora@127.0.0.1
ssh -J root@data-ocean.gamb2le.co.uk -p 2202 aurora@127.0.0.1
```

From a shell already on data-ocean:

```bash
ssh -p 2201 aurora@127.0.0.1
ssh -p 2202 aurora@127.0.0.1
```

Equivalent `~/.ssh/config` aliases:

```sshconfig
Host ass-proxmox-linux-tunnel
  HostName 127.0.0.1
  User aurora
  Port 2201
  ProxyJump root@data-ocean.gamb2le.co.uk

Host aps-proxmox-linux-tunnel
  HostName 127.0.0.1
  User aurora
  Port 2202
  ProxyJump root@data-ocean.gamb2le.co.uk
```

## Safe rollout

1. Generate one dedicated edge-to-data-ocean SSH keypair for the tunnel clients.
2. Add the public key to `edge_tunnel_server_authorized_keys` for
   `aurora-cloud-droplet`.
3. Run this repo against `aurora-cloud-droplet` in check mode first.
4. Apply this repo only after confirming the sshd config include path and
   current SSH access are healthy.
5. Add the private key to the edge repo through Ansible Vault.
6. Add the data-ocean source-sync public key to the edge repo as
   `edge_source_sync_authorized_keys`.
7. Run the edge repo with `edge_managed_write_mode=true` and
   `edge_reverse_tunnels_enabled=true`.
8. Verify listeners on data-ocean:

   ```bash
   ss -ltn '( sport = :2201 or sport = :2202 )'
   ssh -p 2201 aurora@127.0.0.1 hostname
   ssh -p 2202 aurora@127.0.0.1 hostname
   ```
9. After a soak period, switch source sync in this repo with
   `edge_source_sync_use_reverse_tunnels=true` and a configured
   `edge_source_sync_ssh_key_path`.

Do not switch source-sync jobs from Tailscale to these tunnel endpoints until
the tunnels have survived a soak period and the change has been checked against
current operations.
