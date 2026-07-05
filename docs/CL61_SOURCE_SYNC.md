# CL61 Source Sync

## Purpose

CL61 raw NetCDF files are mirrored onto the cloud host so the local raw tree
can become authoritative for retention and downstream archival checks.

## Source And Destination

- Retired source: `aurora@100.117.101.84:/home/aurora/data/cl61`
- Planned source: `aurora-edge-1` after the CL61 instrument move
- Target raw directory: `/project/aurora/raw/cl61`
- Target Zarr: `/data/aurora/products/cl61/gamb2le_depolarisation_lidar_ceilometer_aurora.zarr`
- Quicklooks: `/data/aurora/products/quicklooks/ceilometer`

The original source host answered Tailscale as `celine-edge-1`. That host is
retired and should not be used for new source pulls. The droplet has
`aurora-cl61-source-sync.timer` disabled until CL61 is moved to
`aurora-edge-1` and unattended SSH is authorized there.

The retired source directory contained current NetCDF files written about every
five minutes, plus older retained files. Passwordless SSH from `azimuth` to that
source used `/home/aurora/.ssh/id_ed25519_celine` for the `aurora` service user.

Audit on 2026-05-07 found 10,555 matching NetCDF files using 16G in
`/home/aurora/data/cl61`. The newest files were named for 2026-05-07 and had
approximately five-minute spacing.

## Authentication

The target service user needs unattended SSH access to the active CL61 source.
For the retired `celine-edge-1` source, the check was:

```bash
sudo -u aurora ssh -i /home/aurora/.ssh/id_ed25519_celine aurora@100.117.101.84 true
```

For the `aurora-edge-1` move, update `cl61_source_host`, confirm the source
path and local `aurora` account, authorize the deployed key, then run the same
style of check against the new host before enabling the timer.

Preferred setup after the new source is ready:

1. Run the Ansible playbook. This installs the unit files and generates
   `/home/aurora/.ssh/id_ed25519_celine` if no vault key is supplied.
2. Add `/home/aurora/.ssh/id_ed25519_celine.pub` from the cloud host to
   `aurora@<new-cl61-source>:~/.ssh/authorized_keys` if it is not already
   present.
3. Confirm the SSH test above passes.
4. Run `playbooks/site.yml --check --diff`, then the real playbook.

Alternatively, store an existing source private key in Ansible Vault as
`cl61_source_ssh_private_key_content`.

## Current Deployed Behavior

`aurora-cl61-source-sync.service` runs `/usr/local/bin/aurora-cl61-sync`.
With the current deployment variables, when
`/var/lib/aurora-cloud/cl61-sync.last` does not exist the script writes `0`,
pulls the full current source history, and then advances the state marker on
later runs.

On `2026-07-05`, the live droplet had the CL61 source timer stopped and disabled
because the configured source was still `celine-edge-1`. The infra inventory
pins `cl61_source_sync_timer_enabled: false` for `aurora-cloud-droplet` so a
redeploy does not re-enable dead source pulls. Remove that override only after
the CL61 move to `aurora-edge-1` is complete.

If you deliberately want the old fresh-start behavior again, set
`cl61_source_start_fresh: true` and redeploy. In that mode the script writes
the current epoch and exits when the state file is absent.

To deliberately reset the current sync point, stop the timer and edit or remove
the state file:

```bash
sudo systemctl stop aurora-cl61-source-sync.timer
sudo -u aurora date +%s | sudo tee /var/lib/aurora-cloud/cl61-sync.last
sudo systemctl start aurora-cl61-source-sync.timer
```

## Processing

The CL61 timers are enabled in Ansible:

- `aurora-ceilometer-append.timer`
- `aurora-ceilometer-last24h.timer`
- `aurora-ceilometer-quicklooks.timer`

`aurora-cl61-source-sync.timer` is enabled once source SSH is authorized.

## Legacy Source Push

The source host has an existing user timer,
`~/.config/systemd/user/cl61sync.timer`, which runs
`/home/aurora/scripts/cl61sync.sh`. That script pushes
`/home/aurora/data/cl61/` to `aurora@aurora-cloud:/mnt/data/cl61/` and then
deletes local files older than 21 days after a successful verification.

Do not repair that push script for this build. It targets the old path and
works against the current pull-and-verify model. It was stopped and disabled on
2026-05-07.
