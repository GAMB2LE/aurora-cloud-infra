# Cloud Radar Source Sync

## Source And Destination

- Source: `aurora@100.124.55.22:/home/aurora/data/rpgfmcw94`
- Target raw directory: `/project/aurora/raw/rpgfmcw94`
- Target Zarr: `/data/aurora/products/rpgfmcw94/cloud_radar.zarr`
- Quicklooks: `/data/aurora/products/quicklooks/cloud_radar`

The source stores hourly radar files under a recursive `Yyyyy/Mmm/Ddd/` tree.
The sync pulls `*LV1.NC` files only and preserves relative paths.

## Authentication

Radar source sync uses Tailscale SSH over the tailnet IP without private keys.
The script uses regular `ssh` for rsync compatibility, with identity-file and
public-key authentication disabled:

```bash
sudo -u aurora ssh -o IdentityFile=none -o PubkeyAuthentication=no aurora@100.124.55.22 true
```

## Current Deployed Behavior

`aurora-radar-source-sync.service` runs `/usr/local/bin/aurora-radar-sync`.
With the current deployment variables, when
`/var/lib/aurora-cloud/radar-sync.last` does not exist the script writes `0`,
pulls the full current source history, and then advances the state marker on
later runs.

If you deliberately want the old fresh-start behavior again, set
`radar_source_start_fresh: true` and redeploy. In that mode the script writes
the current epoch and exits when the state file is absent.

To deliberately reset the current sync point, stop the timer and edit or remove
the state file:

```bash
sudo systemctl stop aurora-radar-source-sync.timer
sudo -u aurora date +%s | sudo tee /var/lib/aurora-cloud/radar-sync.last
sudo systemctl start aurora-radar-source-sync.timer
```

## Processing

The radar timers are enabled in Ansible:

- `aurora-radar-append.timer`
- `aurora-radar-quicklooks.timer`

`aurora-radar-source-sync.timer` is enabled once source Tailscale SSH access is
authorized.
