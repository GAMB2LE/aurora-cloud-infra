# Aurora Cloud Infrastructure

Ansible configuration for the AURORA cloud data and presentation hosts.

## What This Repository Owns

- host configuration, nginx, systemd services, and timers
- source synchronization, processing, quicklook, Operations, and archive jobs
- production/development deployment policy and the development live mirror
- cloud-side support for guarded edge reverse tunnels

It does not own dashboard behaviour or native iOS code. Those belong to
[`aurora_cloud_dashboard`](https://github.com/GAMB2LE/aurora_cloud_dashboard)
and
[`aurora-dashboard-ios`](https://github.com/GAMB2LE/aurora-dashboard-ios).

## Operating Model

| Site | URL | Role |
| --- | --- | --- |
| Production | `https://data.gamb2le.co.uk/app` | Stable public service and authoritative live writer on JASMIN. |
| Development | `https://data-ocean.gamb2le.co.uk/app` | Public development service with live mirrored production data. |

Production owns the normal raw and product paths. Development must not run
normal writer timers. Independent `aurora-dev-live-pull-<stage>.timer` units
mirror each raw or product family, while development-only experiments write to
isolated paths. See
[Production and Development](docs/PRODUCTION_DEVELOPMENT.md) for the complete
release, mirror, and rollback policy.

## Backup Contract

Production copies raw and selected derived data additively to both the JASMIN
GWS and the `gamb2le-o` object store. The development mirror and Proxmox guest
backups are useful recovery layers, but neither counts as raw-data retention
evidence. ASS deletes only exact files older than seven days after cloud, GWS,
and object-store verification; APS Power is not pruned. Fresh derived products
not yet archived remain visible as pending uploads until their 30-hour
verification window expires. See
[Backups and Archive Services](docs/ARCHIVE_SERVICES.md) for the
complete status model and [ASS Backup and Retention](docs/ASS_BACKUP_AND_RETENTION.md)
for deletion safeguards.

## Safe First Commands

Run commands from this repository using the pinned `uv` environment:

```bash
uv run ansible-galaxy collection install -r requirements.yml
uv run ansible-playbook playbooks/audit.yml
uv run ansible-playbook playbooks/site.yml --check --diff
```

Do not apply a playbook until its check output, target host, secrets, and
operational impact have been reviewed. Use focused release playbooks for
dashboard-only changes; reserve `playbooks/site.yml` for deliberate host-wide
changes.

## Documentation

- [Operator Quickstart](docs/OPERATOR_QUICKSTART.md): login prerequisites,
  dashboard-first health checks, and incident evidence collection
- [Documentation home](docs/index.md): scope and current deployment contract
- [Production and Development](docs/PRODUCTION_DEVELOPMENT.md): roles, release policy, and rollback
- [Data Locations](docs/DATA_LOCATIONS.md): raw, product, state, and archive paths
- [Backups and Archive Services](docs/ARCHIVE_SERVICES.md): what is copied,
  where it goes, verification windows, repair, alerts, and retention
- [ASS Backup and Retention](docs/ASS_BACKUP_AND_RETENTION.md): fail-closed
  verification gates and the exact edge-pruning boundary
- [Source Syncs](docs/RADAR_SOURCE_SYNC.md): start with the stream-specific guides in the docs navigation
- [Failover](docs/FAILOVER.md): emergency promotion and recovery
- [Reverse Tunnels](docs/REVERSE_TUNNELS.md): guarded cloud-side access setup

The deployed Operations Dashboard is the source of truth for live freshness,
service health, and deployment identity. Documentation describes the intended
contract and must not be used as proof of a current host state.
