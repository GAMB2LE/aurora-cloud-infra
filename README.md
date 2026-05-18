# Aurora Cloud Infrastructure

Ansible configuration for rebuilding the Aurora cloud dashboard host on the existing JASMIN Cloud VM.

## Current Contract

- Public hostname: `data.gamb2le.co.uk`.
- Raw data: `/project/aurora/raw`.
- Dashboard products: `/data/aurora/products`.
- Dashboard app: `/opt/aurora-cloud-dashboard`.
- Public access: `nginx` on `80/443`.
- Private Panel backend: `127.0.0.1:5006` only.
- Panel session policy: websocket keepalive every `15 s`, unused sessions kept
  for `1 h`, and session tokens valid for `24 h` to make short mobile
  backgrounding/reconnect events less disruptive.
- Static dashboard media route:
  `/wxcam-media` maps to `/data/aurora/products/wxcam` so WXcam MP4 playback
  uses normal HTTP range requests rather than the Panel websocket.
- CL61 raw source: `aurora@100.117.101.84:/home/aurora/data/cl61` pulled into `/project/aurora/raw/cl61`.
- Cloud radar raw source: `aurora@100.124.55.22:/home/aurora/data/rpgfmcw94` pulled into `/project/aurora/raw/rpgfmcw94`.
- Vaisala met raw source: `aurora@100.124.55.22:/home/aurora/data/vaisalamet` pulled into `/project/aurora/raw/vaisalamet`.
- ASFS LoggerNet raw source: `aurora@100.124.55.22:/home/aurora/data/asfs/raw/loggernet` pulled into `/project/aurora/raw/asfs/loggernet`.
- ASFS fast-sonic raw source: `aurora@100.124.55.22:/home/aurora/data/asfs/raw/loggernet` pulled into `/project/aurora/raw/asfs/loggernet`.
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
  - `/project/aurora/raw/asfs/loggernet`
  - `/project/aurora/raw/power/level1`
  - `/project/aurora/raw/wxcam`
- Storage type: shared Ceph network filesystem
- Current filesystem size on `2026-05-09`: `4.0T`
- Current used on `2026-05-09`: `36G`
- Current available on `2026-05-09`: `3.9T`

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
- Current filesystem size on `2026-05-09`: `983G`
- Current used on `2026-05-09`: `117G`
- Current available on `2026-05-09`: `816G`

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
The ASFS LoggerNet source sync does the same, restricted to files matching
`asfs-logger_sci_DD_MM_YYYY.dat`.
The ASFS fast-sonic source sync is separate and restricted to files matching
`asfs-logger_fast_sonic_DD_MM_YYYY.dat`; it only builds a Zarr product and is
not exposed in the dashboard.
The power source sync is restricted to files matching `power_data_YYYYMMDD.csv`
and excludes wind-named variables before writing the Zarr product.
The wxcam source sync now mirrors the full raw `FISH/` and `PANO/` tree into
`/project/aurora/raw/wxcam`. Downstream wxcam products still only use the HDR
JPG and HDR MP4 subsets for the catalog, daily videos, thumbnails, and pixel
Zarr.

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
The ASFS LoggerNet source stores flat `asfs-logger_sci_*.dat` files in
`/home/aurora/data/asfs/raw/loggernet`; only the dated science files are synced.
The ASFS fast-sonic source uses the same source directory but syncs only the
dated `asfs-logger_fast_sonic_*.dat` files.
The power source stores flat `power_data_*.csv` files in `/data/power/level1`.
The wxcam source stores nested `FISH/` and `PANO/` trees under
`/home/aurora/data/wxcam`; the deployed sync copies the full tree rather than
filtering by extension so JPG and MP4 products stay together on disk.

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

The collector records:

- source-host disk usage and probe reachability
- local `/project`, `/data`, and `/` filesystem usage
- GWS usage and reachability
- per-stream local and GWS mirror coverage, lag, and mismatch counts
- prune-gate and product-gate summaries
- systemd health for source sync, processing, and transfer units

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
