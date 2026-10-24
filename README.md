# AURORA cloud infrastructure

This repository owns the host configuration, ingestion and archive services,
releases, and production-to-development mirror for AURORA.

I use [JASMIN](https://data.gamb2le.co.uk/app) for operations.
[Data-ocean](https://data-ocean.gamb2le.co.uk/app) is the development site with
mirrored production data. The [operator guide](docs/OPERATOR_QUICKSTART.md)
explains where I look first and how I check the full data path.

- [Documentation home](docs/index.md)
- [Production and development](docs/PRODUCTION_DEVELOPMENT.md): ownership, mirror safeguards and app routing
- [Releases from main](docs/RELEASES.md): exact pins, presentation isolation and rollback
- [Failover](docs/FAILOVER.md): continuity and deliberate writer handover
- [Data locations](docs/DATA_LOCATIONS.md) and [archives](docs/ARCHIVE_SERVICES.md)
- [Dated recovery records](docs/records/index.md): evidence, separate from instructions

Dashboard code lives in [aurora_cloud_dashboard](https://github.com/GAMB2LE/aurora_cloud_dashboard).
The native apps live in [aurora-dashboard-ios](https://github.com/GAMB2LE/aurora-dashboard-ios)
and [aurora-dashboard-android](https://github.com/GAMB2LE/aurora-dashboard-android).
Scientific model evaluation remains owned by
[aurora-model-evaluation](https://github.com/GAMB2LE/aurora-model-evaluation).

Start with read-only, host-limited checks from this repository:

```bash
uv sync --locked
uv run ansible-playbook playbooks/audit.yml --limit aurora-cloud
```

Review the relevant playbook with `--check --diff` before applying it. A
code-only release still changes future scientific jobs if it updates their
shared checkout; use the presentation-only release boundary in the guide.

Documentation is published by the central
[docs portal](https://gamb2le.pages.dev/), triggered by documentation
changes on `main`. It describes the intended contract. Current health requires
fresh host, API and observation evidence.
