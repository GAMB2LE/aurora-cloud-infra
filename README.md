# Aurora Cloud Infrastructure

Ansible configuration for rebuilding the Aurora cloud dashboard host on the existing JASMIN Cloud VM.

## Current Contract

- Public hostname: `data.gamb2le.co.uk`.
- Raw data: `/project/aurora/raw`.
- Dashboard products: `/data/aurora/products`.
- Dashboard app: `/opt/aurora-cloud-dashboard`.
- Public access: `nginx` on `80/443`.
- Private Panel backend: `127.0.0.1:5006` only.
- Fresh CL61 raw source: `aurora@100.117.101.84:/home/aurora/data/cl61` pulled into `/project/aurora/raw/cl61`.
- Fresh cloud radar raw source: `aurora@100.124.55.22:/home/aurora/data/rpgfmcw94` pulled into `/project/aurora/raw/rpgfmcw94`.
- Fresh Vaisala met raw source: `aurora@100.124.55.22:/home/aurora/data/vaisalamet` pulled into `/project/aurora/raw/vaisalamet`.
- Fresh ASFS LoggerNet raw source: `aurora@100.124.55.22:/home/aurora/data/asfs/raw/loggernet` pulled into `/project/aurora/raw/asfs/loggernet`.
- Fresh ASFS fast-sonic raw source: `aurora@100.124.55.22:/home/aurora/data/asfs/raw/loggernet` pulled into `/project/aurora/raw/asfs/loggernet`.
- Fresh power raw source: `aurora@100.81.226.30:/data/power/level1` pulled into `/project/aurora/raw/power/level1`.
- Fresh wxcam raw source: `aurora@100.124.55.22:/home/aurora/data/wxcam` pulled into `/project/aurora/raw/wxcam`.
- GWS backup/sync: rsync via a JASMIN transfer host to `/gws/ssde/j25b/gamb2le`.

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

## Fresh Source Syncs

The CL61 and radar source syncs intentionally do not migrate historical data by
default. The first successful source-sync service run creates its state file with
the current epoch and exits. Later runs pull only source files newer than that
marker.

The Vaisala met source sync starts by pulling all existing matching `.dat` files
so the Zarr can bootstrap from the full current source history.
The ASFS LoggerNet source sync does the same, restricted to files matching
`asfs-logger_sci_DD_MM_YYYY.dat`.
The ASFS fast-sonic source sync is separate and restricted to files matching
`asfs-logger_fast_sonic_DD_MM_YYYY.dat`; it only builds a Zarr product and is
not exposed in the dashboard.
The power source sync is restricted to files matching `power_data_YYYYMMDD.csv`
and excludes wind-named variables before writing the Zarr product.
The wxcam source sync keeps only HDR JPGs and HDR hourly MP4s locally while
leaving the broader source archive untouched upstream. Wxcam indexing builds a
SQLite catalog from the local HDR mirror, daily MP4 products, hourly JPG-based
thumbnail products, and a pixel Zarr that appends from the HDR JPG archive.

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
after a successful verification. Leave that timer disabled for this fresh-start
pull model.

## Secrets

Do not commit secrets. For a first Tailscale registration, pass the auth key from the environment:

```bash
export TAILSCALE_AUTHKEY=...
uv run ansible-playbook playbooks/site.yml --check --diff
```

For unattended GWS sync, create or install a dedicated private key at `/home/aurora/.ssh/id_rsa_jasmin` and authorize it for `rrniii` on the relevant JASMIN transfer service. A forwarded SSH agent from an interactive admin session is not enough for systemd timers.

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
