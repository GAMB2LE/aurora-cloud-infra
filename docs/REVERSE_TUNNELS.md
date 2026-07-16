# Edge Reverse Tunnels

ASS and APS may sit behind Starlink or 5G networks where inbound SSH is not
reliable. The intended replacement transport is therefore edge-initiated reverse
SSH tunnels into `data-ocean.gamb2le.co.uk`.

## Intended Endpoint Map

| Edge host | Data-ocean bind | Target on edge host |
| --- | --- | --- |
| `ass-proxmox-linux` | `127.0.0.1:2201` | `127.0.0.1:22` |
| `aps-proxmox-linux` | `127.0.0.1:2202` | `127.0.0.1:22` |

The matching edge-side client services live in the separate
`GAMB2LE/aurora-edge-infra` repository. Data-ocean only accepts the reverse
forwards.

## Data-Ocean Role

The `edge_tunnel_server` role is wired into `playbooks/site.yml` but disabled by
default:

```yaml
edge_tunnel_server_enabled: false
```

When enabled, it creates a restricted `aurora-tunnel` account and installs the
configured edge public keys with authorized-key options that permit only remote
forwarding to `127.0.0.1:2201` and `127.0.0.1:2202`.

It does not change global `sshd_config` unless this second opt-in is set:

```yaml
edge_tunnel_server_manage_sshd_config: true
```

That explicit opt-in is required because SSH daemon policy changes can affect
current access to data-ocean.

## Rollout Order

1. Add the edge tunnel public key to `edge_tunnel_server_authorized_keys`.
2. Run data-ocean in check mode and verify only the tunnel account/key changes.
3. Apply the data-ocean role.
4. Enable edge-side tunnel services from `aurora-edge-infra`.
5. Verify:

```bash
ss -ltn sport = :2201 or sport = :2202
sudo -u aurora ssh -p 2201 aurora@127.0.0.1 hostname
sudo -u aurora ssh -p 2202 aurora@127.0.0.1 hostname
```

6. Only after tunnel stability is proven should source sync be moved from
Tailscale endpoints to the localhost tunnel ports.

## Safety

Do not enable the source-sync transport switch in the same deploy that first
creates tunnel users or tunnel services. Keep Tailscale available until the
reverse-tunnel path has survived at least one normal collection cycle.
