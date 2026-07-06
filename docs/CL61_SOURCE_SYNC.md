# CL61 Source Sync

## Purpose

CL61 raw NetCDF files are mirrored onto the cloud host so the local raw tree
can become authoritative for retention and downstream archival checks.

## Source And Destination

- Active source: `aurora@100.124.55.22:/home/aurora/data/cl61`
- Retired source: `aurora@100.117.101.84:/home/aurora/data/cl61`
- Target raw directory: `/project/aurora/raw/cl61`
- Target Zarr: `/data/aurora/products/cl61/gamb2le_depolarisation_lidar_ceilometer_aurora.zarr`
- Quicklooks: `/data/aurora/products/quicklooks/ceilometer`

The original source host answered Tailscale as `celine-edge-1`. That host is
retired and should not be used for new source pulls. Current CL61 files are
written on `ass-proxmox-linux` under the shared data drive.

The retired source directory contained current NetCDF files written about every
five minutes, plus older retained files. Passwordless SSH from `azimuth` to that
source used `/home/aurora/.ssh/id_ed25519_celine` for the `aurora` service user.

Audit on 2026-05-07 found 10,555 matching NetCDF files using 16G in
`/home/aurora/data/cl61`. The newest files were named for 2026-05-07 and had
approximately five-minute spacing.

## Authentication

The target service user needs unattended SSH access to the active CL61 source.
The active source uses Tailscale SSH without a private key, matching the other
ASS source streams:

```bash
sudo -u aurora ssh -o IdentityFile=none -o PubkeyAuthentication=no aurora@100.124.55.22 true
```

The retired `celine-edge-1` source used `/home/aurora/.ssh/id_ed25519_celine`.
That key-auth mode is retained in the template behind `cl61_source_auth:
ssh_key`, but the live configuration uses `cl61_source_auth: tailscale`.

## Current Deployed Behavior

`aurora-cl61-source-sync.service` runs `/usr/local/bin/aurora-cl61-sync`.
With the current deployment variables, when
`/var/lib/aurora-cloud/cl61-sync.last` does not exist the script writes `0`,
pulls the full current source history, and then advances the state marker on
later runs.

On `2026-07-06`, CL61 ingest was retargeted to the ASS Linux data path and the
droplet override that disabled `aurora-cl61-source-sync.timer` was removed.

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
