# AURORA operations

I start with [JASMIN](https://data.gamb2le.co.uk/app), check Operations, and then
follow the affected stream from its source to the displayed observation.
[Data-ocean](https://data-ocean.gamb2le.co.uk/app) is the development mirror.
A page response or a recent transfer stamp is not enough to call a stream healthy.

## Start here

| I need to… | Guide |
| --- | --- |
| Check the system and collect useful evidence | [Operator quickstart](OPERATOR_QUICKSTART.md) |
| Understand host ownership and app routing | [Production and development](PRODUCTION_DEVELOPMENT.md) |
| Merge and release reviewed work | [Releases from main](RELEASES.md) |
| Recover service or transfer writer ownership | [Failover](FAILOVER.md) |
| Commission the read-only app fallback | [Operational mobile mirror](OPERATIONAL_MOBILE_MIRROR.md) |
| Find raw data, products and archive evidence | [Data locations](DATA_LOCATIONS.md) |
| Interpret or repair archive status | [Archive services](ARCHIVE_SERVICES.md) and [recovery](ARCHIVE_RECOVERY.md) |
| Check what was actually verified | [Dated recovery records](records/index.md) |

## Keep the boundaries clear

JASMIN owns normal ingestion, products, archives and alerts. Data-ocean mirrors
those products and keeps its experiments in separate paths. A client using the
mirror does not transfer those responsibilities.

The production raw area is `/project/aurora/raw`, with products under
`/data/aurora/products`. Check the actual mounts and quotas: on data-ocean,
`/project` and `/data` share storage. Archives are separate additive copies on
JASMIN GWS and the object store. The development mirror is not retention proof.

Use the stream guides in the navigation for source-specific details, including
[BLE enclosure sensors](BLESENSORS.md), [AURORACam](AURORACAM_SOURCE_SYNC.md),
[Power](POWER_SOURCE_SYNC.md) and [Menapia flights](MENAPIA_FLIGHT_SOURCE_SYNC.md).
[Development experiments](DEVELOPMENT_EXPERIMENTS.md) describes isolated science
and presentation work. The [original rebuild plan](PLAN.md) and dated recovery
records are history, not evidence of the current host state.

This documentation is built by `GAMB2LE/mkdocs-portal`. A local strict build
checks links and navigation; a successful portal deployment and a rendered page
check establish publication.
