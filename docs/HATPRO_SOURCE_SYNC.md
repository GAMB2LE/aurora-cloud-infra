# HATPRO Source Sync

## Source And Destination

- Source: `aurora@100.124.55.22:/home/aurora/data/hatprog5`
- Target raw directory: `/project/aurora/raw/hatprog5`
- Target Zarr: `/data/aurora/products/hatprog5/hatpro.zarr`

The source stores HATPRO files under a recursive `Yyyyy/Mmm/Ddd/` tree. The
sync mirrors all files whose names begin with `HATPROG5-AURORA-ICELAND_`,
including both raw companion files and NetCDF products. The Zarr builder uses
the NetCDF products for:

- `*.LWP.NC`
- `*.IWV.NC`
- `*.IRT.NC`
- non-CMP `*.TPC.NC`
- `*.TPB.NC`
- `*.CMP.TPC.NC`
- `*.MET.NC`

## Authentication

HATPRO source sync uses Tailscale SSH over the tailnet IP without private keys.
The script uses regular `ssh` for rsync compatibility, with identity-file and
public-key authentication disabled:

```bash
sudo -u aurora ssh -o IdentityFile=none -o PubkeyAuthentication=no aurora@100.124.55.22 true
```

## Current Deployed Behavior

`aurora-hatpro-source-sync.service` runs `/usr/local/bin/aurora-hatpro-sync`.
When `/var/lib/aurora-cloud/hatpro-sync.last` does not exist, the script writes
`0`, pulls the full current source history, and then advances the state marker
on later runs.

If you deliberately want a fresh-start behavior, set
`hatpro_source_start_fresh: true` and redeploy. In that mode the script writes
the current epoch and exits when the state file is absent.

To reset the sync point manually:

```bash
sudo systemctl stop aurora-hatpro-source-sync.timer
sudo -u aurora date +%s | sudo tee /var/lib/aurora-cloud/hatpro-sync.last
sudo systemctl start aurora-hatpro-source-sync.timer
```

## Processing

The deployed HATPRO processing timer is:

- `aurora-hatpro-append.timer`
- `aurora-hatpro-quicklooks.timer`

The existing HATPRO builder rewrites the consolidated Zarr from the mirrored raw
tree. This is acceptable for the current HATPRO file volume and keeps the
product deterministic while the instrument has sparse fresh coverage. The
timer runs every 15 minutes because a full rewrite currently takes several
minutes.

The Zarr keeps the standard temperature profile (`T_PROF`) separate from the
composite temperature profile (`T_PROF_CMP`) because those source files can
share timestamps while containing different profile values.
