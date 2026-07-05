# Aurora Cloud Infrastructure

Ansible configuration for building and operating the Aurora cloud dashboard
hosts on JASMIN Cloud and the DigitalOcean droplet.

## Current Contract

- Legacy JASMIN public hostname: `data.gamb2le.co.uk`.
- Active droplet public hostname: `data-ocean.gamb2le.co.uk`.
- Raw data: `/project/aurora/raw`.
- Dashboard products: `/data/aurora/products`.
- Dashboard app: `/opt/aurora-cloud-dashboard`.
- Public access: `nginx` on `80/443`.
- Private Panel backend: `127.0.0.1:5006` only.
- Operations health reports:
  `/data/aurora/products/ops_monitor/health`.
- Panel session policy: websocket keepalive every `15 s`, unused sessions kept
  for `1 h`, and session tokens valid for `24 h` to make short mobile
  backgrounding/reconnect events less disruptive.
- Static dashboard media route:
  `/wxcam-media` maps to `/data/aurora/products/wxcam` so WXcam MP4 playback
  uses normal HTTP range requests rather than the Panel websocket.
- CL61 raw source: disabled on the droplet until CL61 moves from retired `celine-edge-1` to `aurora-edge-1`.
- Cloud radar raw source: `aurora@100.124.55.22:/home/aurora/data/rpgfmcw94` pulled into `/project/aurora/raw/rpgfmcw94`.
- Vaisala met raw source: `aurora@100.124.55.22:/home/aurora/data/vaisalamet` pulled into `/project/aurora/raw/vaisalamet`.
- ASFS Logger CRD raw source: `aurora@100.124.55.22:/home/aurora/data/asfs/raw/crd` pulled into `/project/aurora/raw/asfs/crd`.
- ASFS fast-sonic CRD raw source: `aurora@100.124.55.22:/home/aurora/data/asfs/raw/crd` pulled into `/project/aurora/raw/asfs/crd`.
- ASFS fast-gas CRD raw source: `aurora@100.124.55.22:/home/aurora/data/asfs/raw/crd` pulled into `/project/aurora/raw/asfs/crd`.
- Power raw source: `aurora@100.81.226.30:/data/power/level1` pulled into `/project/aurora/raw/power/level1`.
- WXcam raw source: `aurora@100.124.55.22:/home/aurora/data/wxcam` pulled into `/project/aurora/raw/wxcam`.
- GWS backup/sync: rsync via JASMIN transfer hosts to `/gws/ssde/j25b/gamb2le`.

## Storage layout

The deployed host separates raw mirrored inputs from processed products.

### `/project/aurora`

- Function: raw mirrored source data
- What lives there: synced instrument files pulled from the upstream source
  machines
- Examples:
  - `/project/aurora/raw/cl61`
  - `/project/aurora/raw/rpgfmcw94`
  - `/project/aurora/raw/vaisalamet`
  - `/project/aurora/raw/asfs/crd`
  - `/project/aurora/raw/power/level1`
  - `/project/aurora/raw/wxcam`
- Storage type: shared Ceph network filesystem
- Current filesystem size on `2026-05-18`: `4.0T`
- Current used on `2026-05-18`: `41G`
- Current available on `2026-05-18`: `3.9T`

So `/project/aurora` is the raw landing and mirror area.

### `/data/aurora`

- Function: processed products and dashboard-serving outputs
- What lives there:
  - Zarr stores
  - quicklook PNGs
  - WXcam catalog SQLite
  - WXcam daily videos and thumbnails
  - performance logs and other dashboard products
- Examples:
  - `/data/aurora/products/cl61/...zarr`
  - `/data/aurora/products/rpgfmcw94/cloud_radar.zarr`
  - `/data/aurora/products/quicklooks/...`
  - `/data/aurora/products/wxcam/...`
- Storage type: local disk on `/dev/vdb`
- Current filesystem size on `2026-05-18`: `983G`
- Current used on `2026-05-18`: `197G`
- Current available on `2026-05-18`: `736G`

So `/data/aurora` is the product, work, and output area.

Short version:

- `/project/aurora` = raw source data, shared/networked, large, meant for
  mirrored inputs
- `/data/aurora` = derived products, local, faster/closer to the app, meant
  for Zarrs, plots, catalogs, and media outputs

Why the split is useful:

- raw files stay separate from regenerated products
- products can be deleted and rebuilt without touching the source mirror
- the dashboard reads smaller processed artifacts from local disk instead of
  always working directly from the raw mirror

## Safe First Steps

```bash
uv run ansible-galaxy collection install -r requirements.yml
uv run ansible-playbook playbooks/audit.yml
uv run ansible-playbook playbooks/site.yml --check --diff
```

Do not run `playbooks/site.yml` without `--check` until the old production Git changes have been preserved and transfer/Tailscale secrets have been put in Ansible Vault.

## Parallel Endpoints and Failover

The inventory now includes `aurora-cloud-droplet` at `167.172.54.82`. The
intended public split is:

- `https://data.gamb2le.co.uk`: the legacy JASMIN `aurora-cloud` endpoint.
- `https://data-ocean.gamb2le.co.uk`: the DigitalOcean droplet endpoint.

During the July 2026 JASMIN shutdown window, the droplet can run as the live
processing host on `data-ocean.gamb2le.co.uk` while `data.gamb2le.co.uk`
continues to identify the JASMIN host. Keep only one host running writer timers
at a time. `aurora_failover_role` controls writer behavior:

- `primary` enables source sync, product processing, quicklook, operations, and
  GWS timers.
- `standby` installs the same dashboard stack but keeps writer timers disabled
  and enables `aurora-standby-pull.timer` to pull raw, product, internal, and
  state data from the primary.

As of `2026-07-05`, `aurora-cloud-droplet` is committed as `primary` on
`data-ocean.gamb2le.co.uk`; the standby pull timer is disabled there. Source
pulls to ASS and APS require a Tailscale SSH `accept` policy for Linux user
`aurora`, not an interactive `check` policy. The CL61 source timer is pinned off
on the droplet until the CL61 instrument is moved to `aurora-edge-1`.

The live primary audit on `2026-06-19` measured roughly `95G` under
`/project/aurora/raw`, `457G` under `/data/aurora/products`, and `949M` under
`/data/aurora/internal`. A full warm standby therefore needs a 1TB-class data
disk before replication is enabled.

Full failover is optional. It moves `data.gamb2le.co.uk` to `167.172.54.82`
and runs the droplet with `aurora_domain=data.gamb2le.co.uk` and
`aurora_failover_role=primary`.

See `docs/FAILOVER.md` for deployment, promotion, and failback steps.

## AURORA-LASSO Operational Timer

`aurora-les-operational-run.timer` runs the Cloudnet-centred AURORA-LASSO
daily workflow from the deployed science runtime at
`/data/aurora/les/runtimes/aurora-les-operational-current`.

The managed service uses:

```bash
python -m aurora_les.cli campaign operational-run \
  --campaign configs/campaigns/aurora_leeds_operational_20260521_rolling.yaml \
  --target latest-ready \
  --era5-lag-days 5 \
  --skip-completed \
  --execute \
  --timeout-seconds 86400
```

This selects the newest configured campaign day whose required ERA5 and
observation inputs are present, skips days already recorded as successfully run,
executes the configured command plan, writes the operational summary, writes the
AURORA-LASSO bundle, runs `lasso-check`, and refreshes the campaign index. The
service uses the `cloudnetpy-model-eval` Python runtime because it includes
`xarray` and can validate the MODF/MMDF NetCDF metadata.

Manual dry-run check:

```bash
sudo -u aurora bash -lc 'cd /data/aurora/les/runtimes/aurora-les-operational-current && PYTHONPATH=src /data/aurora/les/runtimes/cloudnetpy-model-eval/bin/python3 -m aurora_les.cli campaign operational-run --campaign configs/campaigns/aurora_leeds_operational_20260521_rolling.yaml --target latest-ready --era5-lag-days 5 --skip-completed --json'
```

## Source Syncs

All configured source syncs now initialize from epoch `0` when their state file
is absent, so the local raw mirror can become authoritative for any stream you
plan to prune upstream.

The CL61 source sync now pulls all currently available matching files and then
advances `/var/lib/aurora-cloud/cl61-sync.last` on later runs.
The radar source sync does the same while preserving the recursive
`Yyyyy/Mmm/Ddd/` source tree.
The Vaisala met source sync pulls all existing matching `.dat` files so the
Zarr can bootstrap from the full current source history.
The ASFS Logger source sync rescans a rolling ten-day CRD window, restricted to
files matching `aurora_asfs_data_sci_YYYYMMDDHHMM.dat` at or after
`202605020000`.
The ASFS fast-sonic source sync is separate, uses the same CRD source directory,
and is restricted to files matching
`aurora_asfs_data_fast_sonic_YYYYMMDDHHMM.dat` at or after `202605020000`; it
only builds a Zarr product and is not exposed in the dashboard selectors.
The power source sync is restricted to files matching `power_data_YYYYMMDD.csv`
and excludes wind-named variables before writing the Zarr product.
The wxcam source sync retains only HDR JPG and MP4 files for the FISH and PANO
streams under `/project/aurora/raw/wxcam`. AUTO/LONG/SHORT files stay on the
camera host and are not cataloged, Zarr-appended, or archived from this VM.

Before enabling this live, confirm SSH from the target works:

```bash
sudo -u aurora ssh -i /home/aurora/.ssh/id_ed25519_celine aurora@100.117.101.84 true
sudo -u aurora ssh -o IdentityFile=none -o PubkeyAuthentication=no aurora@100.124.55.22 true
```

The current audit found Tailscale reachability to `100.117.101.84`
(`celine-edge-1`) and passwordless SSH now works from the `aurora` service user
on `azimuth` using `/home/aurora/.ssh/id_ed25519_celine`. The source contains
fresh files in `/home/aurora/data/cl61`. The radar source at `100.124.55.22`
uses Tailscale SSH without private keys and stores `*LV1.NC` files under a
recursive `/home/aurora/data/rpgfmcw94/Yyyyy/Mmm/Ddd/` tree.
The Vaisala met source at the same tailnet IP stores flat
`vaisala_met_level0_*.dat` files in `/home/aurora/data/vaisalamet`.
The ASFS Logger source stores chunked CRD TOA5 files in
`/home/aurora/data/asfs/raw/crd`; the science sync pulls
`aurora_asfs_data_sci_*.dat` files from the May 2 onward retained window.
The ASFS fast-sonic source uses the same source directory but syncs only the
`aurora_asfs_data_fast_sonic_*.dat` files from the same retained window.
The power source stores flat `power_data_*.csv` files in `/data/power/level1`.
The wxcam source stores nested `FISH/` and `PANO/` trees under
`/home/aurora/data/wxcam`; the deployed sync filters that tree to FISH HDR and
PANO HDR JPG/MP4 files only.

The legacy source-side `cl61sync.timer` on `celine-edge-1` pushes to the old
`aurora-cloud:/mnt/data/cl61` location and prunes local files older than 21 days
after a successful verification. Leave that timer disabled for this pull model.

## GWS Sync and Verification

The deployed transfer model is push-based from this VM, scheduled with
`systemd`, and aimed at the JASMIN GWS layout:

- raw mirror: `/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/`
- products: `/gws/ssde/j25b/gamb2le/data/output/aurora-cloud/products/`
- manifests and logs:
  `/gws/ssde/j25b/gamb2le/data/internal/aurora-cloud/manifests/`

Per-job rsync wrappers try the transfer hosts in this order:

1. `xfer-vm-03.jasmin.ac.uk`
2. `xfer-vm-01.jasmin.ac.uk`
3. `xfer-vm-02.jasmin.ac.uk`

The scheduled jobs are:

- raw mirror push every `5` minutes at `*:01/5`
- core products push every `10` minutes at `*:03/10`, excluding `wxcam/`
- WXcam products push every `30` minutes at `*:07/30`
- manifest push every `10` minutes at `*:06/10`
- mirror verification every `10` minutes at `*:08/10`

The product sync is split because WXcam media and image Zarr products are much
larger than the numeric Zarr and quicklook products. Each wrapper logs its
source, destination, rsync statistics, selected transfer host, and elapsed time
under `/data/aurora/internal/mirror_manifests/logs/`.

The GWS timers also now use a smaller randomized delay (`60` seconds instead of
`300`) so near-real-time streams like CL61 do not sit in an amber “slightly
behind” state for most of each transfer cycle.

The rsync timers are only enabled after the GWS auth probe succeeds. In the
current deployment that probe uses the existing JASMIN RSA key at
`/home/aurora/.ssh/id_rsa_jasmin_20200514`, and the timers then push to the
transfer-host failover chain automatically. Before the first raw/products sync
finishes, mirror verification will honestly report that the GWS raw tree is
missing or incomplete.

Verification writes rolling history under:

- `/data/aurora/internal/mirror_manifests/history/<timestamp>/`
- `/data/aurora/internal/mirror_manifests/latest/`

Each stream gets `source.tsv`, `local.tsv`, optional `gws.tsv`, and
`prune_candidates.tsv`. The manifests include:

- relative path
- size
- mtime
- optional checksum

To avoid false alarms from files that are still actively being written, mirror
verification uses settle windows:

- local mirror comparisons ignore source files newer than `10` minutes
- GWS comparisons ignore source files newer than `45` minutes
- product-gate checks use the newest settled product source older than `15` minutes

`prune_candidates.tsv` is only populated when all of these are true:

- the source file is present
- the local raw mirror matches it
- the GWS raw mirror matches it
- the required product append jobs succeeded through that time window

This is a prune gate and report, not an automatic deletion step.

## Operations Monitoring

The deployed stack now also collects infrastructure and transfer housekeeping
into its own monitoring stream:

- raw snapshots:
  `/project/aurora/raw/ops_monitor/ops_monitor_YYYYMMDD.jsonl`
- latest snapshot:
  `/project/aurora/raw/ops_monitor/latest.json`
- Zarr product:
  `/data/aurora/products/ops_monitor/ops_monitor.zarr`
- quicklooks:
  `/data/aurora/products/quicklooks/ops_monitor/`
- observe-only health reports:
  `/data/aurora/products/ops_monitor/health/`

The collector records:

- source-host disk usage and probe reachability
- local `/project`, `/data`, and `/` filesystem usage
- GWS usage and reachability
- per-stream local and GWS mirror coverage, lag, and mismatch counts
- prune-gate and product-gate summaries
- systemd health for source sync, processing, and transfer units
- dashboard HTTP endpoint health and response time
- dashboard and infrastructure git branch, commit, dirty state, and local
  ahead/behind counts

The health-report layer is Phase 1 observe-only. It summarizes status into
green/amber/red checks, but it does not restart services, delete files, rebuild
stores, or modify code.

The relevant timers are:

- `aurora-ops-monitor-collect.timer` every 5 minutes
- `aurora-ops-monitor-append.timer` every 5 minutes
- `aurora-ops-monitor-quicklooks.timer` every 10 minutes
- `aurora-mirror-verify.timer` every 10 minutes

## Secrets

Do not commit secrets. For a first Tailscale registration, pass the auth key from the environment:

```bash
export TAILSCALE_AUTHKEY=...
uv run ansible-playbook playbooks/site.yml --check --diff
```

For unattended GWS sync, make sure the private key configured by
`gws_ssh_private_key` is readable by the `aurora` service user and already
authorized for `rrniii` on the JASMIN transfer hosts. The live deployment now
uses `/home/aurora/.ssh/id_rsa_jasmin_20200514`. A forwarded SSH agent from an
interactive admin session is not enough for systemd timers.

For CL61 source sync, either let Ansible generate
`/home/aurora/.ssh/id_ed25519_celine` on the target and add its `.pub` file to
the source, or store an existing private key in Ansible Vault as
`cl61_source_ssh_private_key_content`.

Radar source sync uses Tailscale SSH over the tailnet IP and disables private
key authentication in the sync script. No radar SSH private key is installed or
managed by this playbook.
Vaisala met source sync uses the same Tailscale/no-key SSH pattern.
ASFS LoggerNet source sync also uses the same Tailscale/no-key SSH pattern.
ASFS fast-sonic source sync also uses the same Tailscale/no-key SSH pattern.
Power source sync also uses the same Tailscale/no-key SSH pattern.
Wxcam source sync also uses the same Tailscale/no-key SSH pattern.

## Documentation publishing

- This repo carries the three repo-side pieces described in
  `https://gamb2le.pages.dev/documentation-docs/`:
  - `mkdocs.yml`
  - `docs/index.md`
  - `.github/workflows/trigger-docs.yml`
- The `trigger-docs.yml` workflow asks the central `GAMB2LE/mkdocs-portal`
  repo to rebuild the unified site at `https://gamb2le.pages.dev/`.
- The repo-local GitHub Pages workflow has been removed; the central portal is
  the only intended public documentation destination.
- Local docs checks can be run with `python3 check_docs.py`.
- That trigger workflow expects two GitHub Actions secrets in this repo:
  - `APP_ID = 2899200`
  - `APP_PRIVATE_KEY = <the GitHub App private key from the docs process>`
- The central portal repo still has to include this repository in its own
  `mkdocs.yml` nav and its docs-clone workflow, exactly as described in the
  unified docs instructions.
