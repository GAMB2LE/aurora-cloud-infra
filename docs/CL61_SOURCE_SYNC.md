# CL61 Source Sync

## Purpose

CL61 raw NetCDF files are mirrored onto the cloud host so the local raw tree
can become authoritative for retention and downstream archival checks.

## Source And Destination

- Source: `aurora@100.117.101.84:/home/aurora/data/cl61`
- Target raw directory: `/project/aurora/raw/cl61`
- Target Zarr: `/data/aurora/products/cl61/gamb2le_depolarisation_lidar_ceilometer_aurora.zarr`
- Quicklooks: `/data/aurora/products/quicklooks/ceilometer`

The source host answers Tailscale as `celine-edge-1`. The audited CL61 source
directory contains current NetCDF files written about every five minutes, plus
older retained files. Passwordless SSH from `azimuth` to the source is available
for the `aurora` service user using `/home/aurora/.ssh/id_ed25519_celine`.

Audit on 2026-05-07 found 10,555 matching NetCDF files using 16G in
`/home/aurora/data/cl61`. The newest files were named for 2026-05-07 and had
approximately five-minute spacing.

## Authentication

The target service user needs unattended SSH access to the source:

```bash
sudo -u aurora ssh -i /home/aurora/.ssh/id_ed25519_celine aurora@100.117.101.84 true
```

Preferred setup:

1. Run the Ansible playbook. This installs the unit files and generates
   `/home/aurora/.ssh/id_ed25519_celine` if no vault key is supplied.
2. Add `/home/aurora/.ssh/id_ed25519_celine.pub` from `azimuth` to
   `aurora@100.117.101.84:~/.ssh/authorized_keys` if it is not already present.
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

The radar timers are already enabled in the live deployment. HATPRO timers
remain disabled until its raw source and product pipeline are configured.

## Legacy Source Push

The source host has an existing user timer,
`~/.config/systemd/user/cl61sync.timer`, which runs
`/home/aurora/scripts/cl61sync.sh`. That script pushes
`/home/aurora/data/cl61/` to `aurora@aurora-cloud:/mnt/data/cl61/` and then
deletes local files older than 21 days after a successful verification.

Do not repair that push script for this build. It targets the old path and
works against the current pull-and-verify model. It was stopped and disabled on
2026-05-07.
