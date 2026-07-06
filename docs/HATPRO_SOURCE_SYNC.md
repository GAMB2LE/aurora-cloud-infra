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

The HATPRO builder appends new samples by default. Each timer run scans a
bounded lookback window before the current Zarr frontier, prefers canonical
`Yyyyy/Mmm/Ddd/` files over old top-level mirror duplicates, and appends only
samples newer than the product. Use `hatpro_to_zarr.py --rebuild` only for an
intentional full product rewrite.

Mirror verification also ignores legacy top-level HATPRO duplicates and compares
only the canonical recursive raw tree against source and GWS manifests. If a
source batch was mirrored flat before this policy, run one preservation rsync
from the source root to backfill the missing `Yyyyy/Mmm/Ddd/` paths before
allowing pruning.

The Zarr keeps the standard temperature profile (`T_PROF`) separate from the
composite temperature profile (`T_PROF_CMP`) because those source files can
share timestamps while containing different profile values.
