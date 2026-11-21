# Recovery tunnel rollout and rollback

This is a focused management-access change. Run one named cloud host at a time;
keep an existing trusted administrative session open. Do not apply the complete
site playbook or switch source-sync transport as part of this rollout.

## Prepare and validate

1. Record existing account state, the tunnel SSH fragment, authorized keys,
   service state and listeners. Back up any files that will change. Verify the
   public cloud destination is reachable from each field uplink and satisfies
   that provider's ingress rules. JASMIN source-address policy requires separate
   review; this role does not alter it or any firewall.
2. Prepare distinct machine/cloud public keys, exact listener assignments, and
   trusted field ED25519 fingerprints. Keep private keys outside this repository.
   Configure the server variables using `docs/examples/reverse_tunnel_vars.yml`.
   Preserve the legacy shared key during initial migration.
3. Parse and inspect the focused change:

   ```bash
   uv run --locked ansible-playbook playbooks/edge_tunnel_server.yml --syntax-check
   uv run --locked ansible-playbook playbooks/edge_tunnel_server.yml --check --diff --limit aurora-cloud-droplet -e @/secure/path/cloud-vars.yml
   ```

4. Apply the same focused command without `--check --diff`. The role validates
   both full configuration and the effective restricted user policy before a
   validated SSH reload and key installation. Repeating the role deliberately
   reloads validated SSH policy before key changes, avoiding a retry that installs
   credentials against a configuration not yet active in the running daemon.
5. Repeat for `--limit aurora-cloud` with its own four public keys. Deploy the
   corresponding independent clients using the edge repository's focused role.

## Commission and observe

- Confirm the six listeners bind only `127.0.0.1`; authenticate to all four SSH
  endpoints and record hostname/machine identity. Fingerprint checks are necessary
  but not sufficient when cloned guests share a host key.
- Verify tunnel identities cannot execute a command, allocate PTY, open a local
  forward, create a StreamLocal forward, or bind another machine's cloud port.
- From the existing operator computer, open local SSH forwards through the
  selected cloud: `127.0.0.1:14001` to the cloud's `127.0.0.1:2401` and
  `127.0.0.1:14002` to `127.0.0.1:2402`. Connect the local NoMachine client to
  ports 14001/14002 using NX over TCP. Confirm authenticated, responsive
  desktops separately from SSH; no cloud desktop or NoMachine install is needed.
- Verify client reconnects after bounded interruptions, and test each cloud
  independently. Observe for 24 hours using the opt-in status timer, including
  actual logins and desktop checks at commissioning and acceptance. Runtime
  failures must remain visible; an active client alone is not acceptance.
- Only after the new DigitalOcean guest sessions work and both relays pass
  authenticated SSH checks, set `edge_tunnel_server_replace_authorized_keys: true` and reapply
  the focused server playbook to revoke the preserved legacy key.

## Rollback

Restore only the backed-up tunnel fragment and tunnel account's authorized-key
file, preserving owner and mode. If the fragment was newly created, remove only
that fragment. Run `sudo sshd -t` before `sudo systemctl reload ssh`; keep the
existing admin session open and re-test it before closing. The role automatically
restores its previous fragment if initial validation fails, before any new key
installation. An explicit reload failure stops the apply before authorizing keys;
inspect the saved backup and daemon state before retrying.

If the observation timer was newly enabled, disable only
`aurora-edge-access-status.timer`. Roll back new field clients using the edge
runbook; keep the old working guest tunnel until its replacement was accepted.
Do not remove a pre-existing account, overwrite unrelated SSH settings, flush
firewall rules, stop Tailscale, change data collection or switch source-sync.
