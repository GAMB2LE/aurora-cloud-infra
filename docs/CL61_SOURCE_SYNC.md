# CL61 Source Sync

## Purpose

CL61 raw NetCDF files should start fresh on the cloud host. Historical data from
the restored filesystem backup is intentionally not copied.

## Source And Destination

- Source: `aurora@100.117.101.84:/mnt/data/cl61`
- Target raw directory: `/project/aurora/raw/cl61`
- Target Zarr: `/data/aurora/products/cl61/gamb2le_depolarisation_lidar_ceilometer_aurora.zarr`
- Quicklooks: `/data/aurora/products/quicklooks/ceilometer`

Confirm the source path after SSH access is fixed. The source host currently
answers Tailscale as `celine-edge-1`, but SSH authentication is not yet
accepted for `aurora`.

## Authentication

The target service user needs unattended SSH access to the source:

```bash
sudo -u aurora ssh -i /home/aurora/.ssh/id_ed25519_celine aurora@100.117.101.84 true
```

Preferred setup:

1. Run the Ansible playbook once with `cl61_source_sync_timer_enabled: false`.
   This installs the unit files and generates
   `/home/aurora/.ssh/id_ed25519_celine` if no vault key is supplied.
2. Add `/home/aurora/.ssh/id_ed25519_celine.pub` from `azimuth` to
   `aurora@100.117.101.84:~/.ssh/authorized_keys`.
3. Confirm the SSH test above passes.
4. Set `cl61_source_sync_timer_enabled: true`.
5. Run `playbooks/site.yml --check --diff`, then the real playbook.

Alternatively, store an existing source private key in Ansible Vault as
`cl61_source_ssh_private_key_content`.

## Fresh-Start Behavior

`aurora-cl61-source-sync.service` runs `/usr/local/bin/aurora-cl61-sync`.
When `/var/lib/aurora-cloud/cl61-sync.last` does not exist, the script writes
the current epoch and exits. This prevents accidental historical backfill.

To deliberately reset the fresh-start point, stop the timer and edit or remove
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

`aurora-cl61-source-sync.timer` is installed but intentionally gated by
`cl61_source_sync_timer_enabled` until source SSH is authorized.

Radar and HATPRO timers remain disabled until their fresh sources are configured.
