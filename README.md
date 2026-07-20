# Aurora Cloud Infrastructure

Ansible configuration for the AURORA dashboard cloud hosts.

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
normal writer timers: it uses `aurora-dev-live-pull.timer` and development-only
paths for experiments. See [Production and Development](docs/PRODUCTION_DEVELOPMENT.md)
for the complete release, cutover, and rollback policy.

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

- [Documentation home](docs/index.md): scope and current deployment contract
- [Production and Development](docs/PRODUCTION_DEVELOPMENT.md): roles, release policy, and rollback
- [Data Locations](docs/DATA_LOCATIONS.md): raw, product, state, and archive paths
- [Source Syncs](docs/RADAR_SOURCE_SYNC.md): start with the stream-specific guides in the docs navigation
- [Failover](docs/FAILOVER.md): emergency promotion and recovery
- [Reverse Tunnels](docs/REVERSE_TUNNELS.md): guarded cloud-side access setup

The deployed Operations Dashboard is the source of truth for live freshness,
service health, and deployment identity. Documentation describes the intended
contract and must not be used as proof of a current host state.
