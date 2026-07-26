# Archive Services

`aurora-cloud-infra` is the authoritative repository for every service that
pulls edge data to the cloud, mirrors it to the GWS or object store, verifies
archive parity, publishes archive health, or coordinates retention.

## Repository boundaries

| Responsibility | Repository |
| --- | --- |
| ASS and APS host configuration and restricted delete helper | `aurora-edge-infra` |
| Edge-to-cloud source sync, GWS mirror, object-store mirror, verification, retention orchestration, archive monitoring | `aurora-cloud-infra` |
| Read-only presentation of archive health | `aurora_cloud_dashboard` |

The dashboard must not install transfer, verification, or pruning services.
It consumes `/data/aurora/internal/archive_status/health-v1.json`.
Its generic operations collector may copy contract metrics into presentation
snapshots for compatibility, but it must not SSH-probe the GWS, parse archive
manifests, inspect archive writer units, or infer prune readiness.

## Canonical stream catalogue

`inventory/group_vars/aurora_cloud.yml` contains `aurora_archive_streams`.
Each entry defines the edge source, cloud raw root, archive-relative path,
retention duration, and whether pruning is allowed. Add a stream there before
adding service-specific logic.

Radar (`rpgfmcw94`) and APS power pruning are disabled. They remain disabled
until their complete histories are independently proved in both archives.

## Data flow

```text
ASS / APS
    |
    | source-sync services
    v
cloud raw mirror
    |                       |
    | GWS mirror            | additive object-store writers
    v                       v
JASMIN GWS              object store
    |                       |
    +---- fresh manifests --+
                |
                v
      health-v1.json and exact retention permit
```

Writers never wait for verification and never delete destination data. In
particular, the GWS rsync wrapper deliberately omits every `--delete` mode so
source retention cannot propagate into the archive.
Verification observes writers; it does not enable or disable them.

The full-history raw writer interleaves two bounded phases on every activation:
the newest two days are copied first, then a full-history backfill slice runs.
This prevents a multi-terabyte backlog from starving current observations.
High-cardinality product, camera, and manifest writers use the same two-phase
pattern: a short newest-first slice followed by a bounded full-history slice.
Thus every timer cycle reconsiders newly published chunks while historical
gaps continue to converge instead of falling permanently outside a lookback.
Full product inventories list each product family in smaller parallel shards;
they never depend on one unbounded recursive object-store listing.

When an inventory publishes exact missing or mismatched paths,
`aurora-object-store-repair.path` starts the catalogue-driven repair service.
It revalidates every settled source file, rejects symlinks and paths outside
the configured source root, orders candidates newest first, and performs only
exact `rclone copy --files-from-raw` operations. It never deletes or broadly
rewalks the archive to repair a known finite gap.
An outer graceful GNU `timeout` enforces each wall-clock budget because the
deployed legacy rclone can stop transfers yet continue scanning after its own
`--max-duration` deadline.

## Playbooks

- `playbooks/archive_services.yml` configures the complete archive stack.
- `playbooks/gws_mirror.yml` configures the additive GWS writer and verifier.
- `playbooks/object_store_mirror.yml` configures only non-destructive object
  storage and archive-health publication.
- `playbooks/retention_coordinator.yml` installs the fail-closed cloud
  coordinator while allowing its timer to remain explicitly disabled.
- `playbooks/archive_dashboard_consumer.yml` releases only the pinned dashboard
  consumer and points it at the read-only health contract. It does not install
  or reapply the contract producer, source-sync, archive writer, verifier, or
  retention roles.
- `playbooks/dashboard_runtime_release.yml` deliberately excludes every archive
  role. Use `archive_services.yml` for archive changes and `site.yml` for a
  complete host rebuild.

Always run `--check --diff` before applying a playbook.

## Monitoring contract

`aurora-archive-health.timer` publishes schema `health-v1` every two minutes.
The contract contains:

- per-stream GWS missing and mismatch counts;
- per-stream coverage and retention readiness;
- raw object-store missing and mismatch counts;
- writer and verifier unit states;
- verification timestamps and the object-store clean streak.

Missing or stale verification must be treated as unsafe. A red health result
does not stop the additive writers; it only blocks pruning and alerts operators.

The `health-v1` producer is the only code allowed to turn archive evidence into
operator state. Browser, mobile API, reports, and notification code are
read-only consumers of that result.
