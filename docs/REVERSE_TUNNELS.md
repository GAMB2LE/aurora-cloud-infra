# Edge recovery SSH and NoMachine transports

The optional `edge_tunnel_server` role supports independent recovery endpoints on
`aurora-cloud-droplet` (DigitalOcean) and `aurora-cloud` (JASMIN). Each of the four
field machines opens its own outbound SSH session to each cloud server. A guest
failure therefore does not remove its Proxmox host's own recovery connection.
The clients are managed in `GAMB2LE/aurora-edge-infra`.

| Listener on either cloud server | Field destination |
| --- | --- |
| `127.0.0.1:2201` | ASS Linux SSH, `127.0.0.1:22` |
| `127.0.0.1:2202` | APS Linux SSH, `127.0.0.1:22` |
| `127.0.0.1:2211` | ASS Proxmox SSH, `127.0.0.1:22` |
| `127.0.0.1:2212` | APS Proxmox SSH, `127.0.0.1:22` |
| `127.0.0.1:2401` | ASS Linux NoMachine, `127.0.0.1:4000` |
| `127.0.0.1:2402` | APS Linux NoMachine, `127.0.0.1:4000` |

The operator starts from an existing cloud shell or desktop. These listeners are
loopback only; no recovery port is opened to the public network. The two clouds
receive independent field connections. Source-sync transport, data writers,
Tailscale, field routing, existing cloud ingress and public firewalls are not
changed by this role. Field power, Internet egress and cloud availability remain
requirements.

## Server configuration

The role remains disabled by default. Enabling it requires explicit named public
keys and the managed SSH restrictions. Each key entry has `name`, `key` (ED25519
public key only) and `listen_ports` (an exact subset of the six loopback endpoints).
Use a separate key for each machine/cloud pair. A Linux key receives its SSH and
NX listener; a Proxmox key receives only its own SSH listener. Arbitrary key
options and non-loopback listeners are rejected.

The `aurora-tunnel` account has no shell/session channel, password, PTY, agent,
X11, StreamLocal or tunnel-device access. Only remote TCP forwarding is allowed;
local forwarding is forbidden. Server-side alive checks are 30 seconds with a
count of three. `Match all` closes the restriction block so later configuration
is not accidentally scoped to the tunnel user.

The role backs up changed configuration and authorized-key files. It validates
the full `sshd` configuration and effective tunnel-user restrictions before
activating them. Failed validation restores the previous fragment without
reloading SSH. A validated SSH reload happens before new keys are installed,
including on retries, and does not flush unrelated Ansible handlers.

`edge_tunnel_server_replace_authorized_keys: false` preserves existing unmanaged
keys while maintaining an authoritative managed block. Removing an entry revokes
it from that managed block. After migration acceptance, set the variable to
`true` to replace the complete file and remove the legacy shared key. Maintain
at least one authorized machine key while the role is enabled.

## Read-only transport status

Set `edge_tunnel_server_expected_host_fingerprints` to a map of `ass-linux`,
`aps-linux`, `ass-proxmox` and `aps-proxmox` to their independently verified
ED25519 SHA256 fingerprints. Obtain these through existing trusted access;
never establish trust from the recovery endpoint being tested. Cloned host keys
must be recorded honestly: a matching fingerprint alone cannot distinguish
machines sharing that key. Commissioning must separately authenticate and check
hostname/machine identity.

Run `/usr/local/bin/aurora-edge-access-status`. It checks all six loopback
endpoints concurrently, emits JSON, and uses exit 0 for verified SSH handshakes
plus open NX TCP paths, 1 for degraded transports, and 2 for invalid configuration.
Missing fingerprints are unverified. Authentication and rendered desktop access
are always explicitly `not_tested`. NX checks detect immediate downstream
connection closure; a silent open TCP path remains application-unverified.
Separate SSH and NX status fields prevent a stopped NoMachine service from
misreporting the SSH recovery path as down.

`edge_tunnel_server_status_timer_enabled: true` enables an unprivileged five-minute
observation timer. The bounded service has a 30-second timeout and records JSON
in the journal; it holds no login key and does not run an SSH session or desktop.
Use `journalctl -u aurora-edge-access-status.service` for observation records.

See [rollout and rollback](REVERSE_TUNNEL_ROLLOUT.md) and the
[nonsecret variables example](examples/reverse_tunnel_vars.yml).
