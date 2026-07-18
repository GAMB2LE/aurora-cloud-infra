# AURORACam Source Sync

- Source: `aurora@100.124.55.22:/home/aurora/data/mx4`
- Target raw directory: `/project/aurora/raw/auroracam`
- Metadata Zarr: `/data/aurora/products/auroracam/auroracam.zarr`
- Dashboard static route: `/auroracam-media`

AURORACam is the four-camera MOBOTIX M24 archive fed by FTP on
`ass-proxmox-linux`. The cameras write one JPEG per minute into per-camera day
folders:

```text
/home/aurora/data/mx4/<camera>/YYYY-MM-DD/<camera>_YYYY-MM-DD_HH-MM.jpg
```

The cloud uses two independent transfer lanes. The **priority live lane** runs
every two minutes, transfers recent JPEGs first, and advances its checkpoint
only after rsync succeeds. The **archive lane** runs at lower I/O/network
priority, excludes the most recent hour, and copies a bounded newest-first
batch of older files on each run. This guarantees that an archive recovery
cannot delay current station frames. Older `NNN-HH-MM-SS.jpg` test files are
not transferred.

## Cameras

| Camera | IP |
| --- | --- |
| `end-south-array-cam` | `192.168.1.27` |
| `fence-post-cam` | `192.168.1.28` |
| `radar-cam` | `192.168.1.29` |
| `mid-south-array-cam` | `192.168.1.30` |

## Dashboard behavior

- Dashboard tab name: `AURORACam`
- Latest view: four latest JPEGs for the selected day
- Camera view: selected camera with a UTC hourly still strip
- JPEG serving: browser URLs under `/auroracam-media/...`
- Metadata product: small Zarr index of file paths, camera IDs, times, sizes,
  and mtimes

The full-resolution JPEGs remain in the raw mirror. The Zarr is intentionally a
metadata index, not a raw-pixel store, so it can be rebuilt frequently without
duplicating QXGA image payloads.

## Authentication

The sync uses Tailscale SSH over the tailnet. The rsync remote shell is regular
`ssh` with identity keys disabled:

- `IdentitiesOnly=yes`
- `IdentityFile=none`
- `PubkeyAuthentication=no`
- `StrictHostKeyChecking=accept-new`

No private key is installed for this source.

## Timers

- `aurora-auroracam-source-sync.timer`
- `aurora-auroracam-backfill.timer`
- `aurora-auroracam-index.timer`

The priority and archive lanes use separate locks and checkpoints under
`/var/lib/aurora-cloud/`, so they can progress independently. The archive
cursor moves from newer to older files only after each bounded batch succeeds.
