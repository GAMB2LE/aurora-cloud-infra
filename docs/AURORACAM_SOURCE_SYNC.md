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

The cloud source sync copies only the four expected camera folders and files
matching that filename shape. It scans the current and previous UTC date
folders, transfers files newer than its checkpoint with a ten-minute overlap,
and advances the checkpoint only after rsync succeeds. A missing, invalid, or
older-than-window checkpoint resumes at the live edge; historical gaps use a
separate backfill. Older `NNN-HH-MM-SS.jpg` test files are not transferred.

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
- `aurora-auroracam-index.timer`

The sync script uses `/var/lib/aurora-cloud/auroracam-sync.lock` so overlapping
rsync timer runs exit cleanly. Its checkpoint is
`/var/lib/aurora-cloud/auroracam-sync.last`.
