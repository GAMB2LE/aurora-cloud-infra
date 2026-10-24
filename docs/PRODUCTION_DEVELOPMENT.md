# Production and development

I use JASMIN first. Data-ocean is the development site and a copy of production
data. A working page tells me the application is running; I still check the
observation times before saying the data are current.

| Surface | Address | Purpose |
| --- | --- | --- |
| Production dashboard | `https://data.gamb2le.co.uk/app` | Normal operational browser view. |
| Production mobile API | `https://data.gamb2le.co.uk/mobile/v1` | Default for production iOS and Android apps. |
| Development dashboard/API | `https://data-ocean.gamb2le.co.uk/app` and `/mobile/v1` | Development views, including isolated candidate products. |
| Operational mobile mirror | `https://data-ocean.gamb2le.co.uk/mobile/operational/v1` | Dedicated read-only fallback of production products, when commissioned. |

The operational mirror is separately enabled. Its presence in this guide does
not establish that it is deployed or fresh. The dated recovery record holds
that evidence; see [Recovery records](records/index.md).

## Who owns the data

JASMIN `aurora-cloud` owns normal source syncs, product generation, Operations,
alerts, GWS/object-store archives and retention coordination. Production uses
`aurora_site_env: production`; development uses `development`, with normal
writer timers disabled. `aurora_failover_role` remains a compatibility setting.

Data-ocean `aurora-cloud-droplet` pulls production data in separate
`aurora-dev-live-pull-<stage>.timer` jobs. Most run every five minutes; AURORACam
and BLE stages run every two minutes. One slow stream must not hold the others.
Its ordinary writers are confined to `/project/aurora/dev-raw` and
`/data/aurora/dev-products`. Model evaluation is owned by its separate repository.
See [Development experiments](DEVELOPMENT_EXPERIMENTS.md).

Raw observations land in `/project/aurora/raw`; products are in
`/data/aurora/products`. These paths are mirrored. The mirror is a continuity
copy, never archive parity or permission to prune field data.

## Mirror and recovery rules

Every transfer checks the source directory and the source/destination mount.
Raw observations, service state and outage-only files are additive. Inventory
starts with deletion off and reconciliation on. After reviewed dry runs and
outage reconciliation, deletion can be enabled only for explicitly named
`product-dashboard` or `product-quicklooks` replicas. Their mounted source must
also be nonempty. Raw, Power, internal and state paths cannot opt into deletion.

For an existing host, `playbooks/mirror_transport_release.yml` replaces only
the transport used by the installed stage timers. Preserve the old wrapper and
supply its SHA-256 as `aurora_mirror_transport_expected_sha256`; review with
`--check --diff` first. The playbook requires additive reconciliation settings,
the existing planning-cache bind and its exact reviewed guard. It does not
alter schedules or credentials. A process already running finishes with its
old wrapper; verify the next scheduled stages before calling this deployed.

`--partial` and `--delay-updates` reduce exposure to incomplete files; they do
not make a whole multi-file Zarr transfer atomic. BLE holds its producer lock.
Always validate complete products and observation times before routing clients.
Keep existing development experiments and the relocated Power cache guard intact.

Each stage records its own success under
`/var/lib/aurora-cloud/dev-live-mirror`. The public
`/data/aurora/internal/dev-live-mirror/last_success.json` describes the dashboard
stage, not every stream. Compare source, raw, product, API and media frontiers.

## What the apps do

Production apps start with JASMIN. A failed connection or HTTP 5xx response may
use the dedicated operational mirror, labelled **Development mirror** with its
data age. Authentication, certificate and schema failures do not trigger this
fallback. Custom servers do not silently redirect to public infrastructure.
The ordinary development API is not an automatic fallback: it may present
candidate Power output. Related responses and media stay on one deployment;
cached data remain labelled cached. Normal refresh tries JASMIN again.

Automatic client fallback does not move writer, archive, alert or retention
ownership. [Failover](FAILOVER.md) covers an actual ownership transfer.

## Release policy

Completed work belongs on `main`; hosts run exact reviewed pins. Start with
[Releases](RELEASES.md) and use an isolated presentation checkout when source
integration includes scientific changes that have not been promoted.

I accept recovery after two successful scheduled cycles for each active stream,
advancing observation times, explained intentional gaps and completed archive
verification. Storage must cover existing service reserves and seven days of
measured net growth. If that is not possible while preserving the observations,
I hold the cutover and record the capacity needed. Retention stays paused until
its existing verification gates pass.
