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
Raw inventories use the same rule for every family, including the multi-terabyte
radar archive. Families are scheduled independently, radar is listed as bounded
year/month subtrees, and a global process semaphore limits nested listings to
the configured `object_store_inventory_process_limit` (16 in production,
matching the cloud host's CPU count).
Model-evaluation campaign data has independent additive writers to both GWS
and object storage; it is not implicitly covered by the products job.
Symlinked runtime inputs are dereferenced by both writers and verified as
regular, restorable files under their campaign-relative paths.

WXCam's live `wxcam.zarr` is a mutable derived working store and is
intentionally excluded from both product archives. Its immutable raw HDR
imagery, catalogue, daily videos, and hourly thumbnails remain covered.
Because the Zarr is reproducible from archived imagery, it is never accepted
as retention evidence.

When an inventory publishes exact missing or mismatched paths,
`aurora-object-store-repair.path` starts the catalogue-driven repair service.
It revalidates every settled source file, rejects paths outside the configured
source root, follows symlinks only for jobs explicitly marked `copy_links`,
orders candidates newest first, and performs only
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
- per-job and aggregate object-store missing and mismatch counts;
- per-job and aggregate direct GWS missing and mismatch counts for raw,
  products, WXCam products, model evaluation, and manifests;
- every catalogue stream's source-sync timer and service state, including the
  independent radar and AURORACam historical backfill lanes;
- raw, product, WXCam product, model-evaluation, and manifest GWS writer states;
- every GWS/object-store writer and verifier service, timer, repair path, and
  verification-gate path state;
- verification timestamps and the object-store clean streak.

The inventory itself atomically updates
`/data/aurora/internal/object_store_manifests/progress.json` with its state,
current job, current phase, completed jobs, and total job count. A heartbeat
refreshes `updated_at` every minute even while one historical object-store
listing is still running. The health contract embeds that evidence and
publishes a numeric running-state metric, so consumers can distinguish a slow
healthy scan from a stalled one without inspecting processes or inferring
progress from partial manifests. While the inventory service is running, a
heartbeat older than five minutes is an authoritative health failure.
Complete inventories are retained as immutable history, bounded by
`object_store_inventory_history_keep` (12 reports in production). This
preserves multiple independent proof runs without allowing large TSV evidence
snapshots to grow without limit.
Operational transfer logs are local diagnostics, not archive data. Both
manifest writers explicitly exclude the root `logs/` tree; any legacy
additive copies are ignored by parity checks and are never retention evidence.

Missing or stale verification must be treated as unsafe. A red health result
does not stop the additive writers; it only blocks pruning and alerts operators.

The `health-v1` producer is the only code allowed to turn archive evidence into
operator state. Browser, mobile API, reports, and notification code are
read-only consumers of that result. This includes source-sync systemd health:
dashboard collectors must not keep a second list of source-sync units or probe
those units directly.

A cloud-product writer failure therefore cannot be hidden by clean raw-data
evidence, and aggregate object parity cannot be green while a non-raw job still
has a gap.

## Safe convergence runbook

Run these on the cloud host. None of them enables pruning:

```bash
systemctl status aurora-mirror-verify.service
systemctl status aurora-object-store-inventory.service
systemctl status aurora-object-store-repair.path
systemctl status aurora-object-store-verification-gate.path
systemctl is-enabled aurora-ass-retention.timer
```

1. Leave every additive writer timer running.
2. Start one full inventory with
   `sudo systemctl start --no-block aurora-object-store-inventory.service`.
3. Wait for atomic publication at
   `/data/aurora/internal/object_store_manifests/latest/comparison.json`.
   The contract keeps the two archive layouts explicit: object storage
   preserves settled cloud-ingress relative paths, while raw data on GWS uses
   the canonical per-stream `Y/M/D` hierarchy. `source_vs_s3` therefore proves
   cloud-to-object parity, and `source_vs_gws` is built from the independent
   canonical edge-source/GWS manifests for raw streams. Products, WXCam
   products, model evaluation, and manifests use an independent direct GWS
   inventory through a JASMIN transfer host. The service never compares the
   two deliberately different raw path layouts directly. The verifier publishes
   its complete `latest/` tree by atomic directory replacement, so readers
   cannot combine manifests from different verification runs. Object parity
   excludes symlink pointers and re-stats every source file after the remote
   listing; anything changed during that window is deferred to a later run
   rather than reported as a destructive size mismatch.
   The parity snapshot also trails live object writers by six hours (and
   continuously changing verifier manifests by two hours). This remains well
   inside the seven-day edge-retention horizon while exceeding writer cadence
   plus a complete inventory run. Raw GWS parity is read from one immutable
   verifier history directory and applies the same settled-source cutoff as
   the authoritative stream verifier.
   `verification_settle_age` controls only this proof horizon. The independent
   `settle_age` used by additive copy writers remains 15 minutes for raw,
   20 minutes for products, 30 minutes for WXCam products, one hour for model
   evaluation, and five minutes for manifests; verification must never slow
   delivery of fresh data.
4. The repair path unit copies only the exact reported missing or mismatched
   paths. It must finish successfully before another inventory is started.
5. Run a fresh inventory. A report is clean only when every job has zero gaps
   and mismatches against both GWS and object storage. It then establishes
   clean streak one.
6. Run another independent full inventory. Only that distinct second clean
   report may establish stable parity.
7. Confirm `health-v1.json` is green and review both cloud and edge audit logs
   before any dry-run retention canary.

`aurora-ass-retention.timer` stays disabled until the restricted edge helper is
installed with explicit approval and a dry-run canary has been reviewed. A
failed, interrupted, or partial inventory never replaces the previous complete
comparison and can never authorize deletion.

Retention permits are signed by the cloud coordinator using
`ass_retention_signing_private_key`; edge helpers trust only the corresponding
root-managed public key. Never commit either private-key material or an
unencrypted key variable. Each permit is also built from the immutable GWS
history directory named by its verification timestamp, never from a sequence
of reads against the moving `latest` pointer. Missing signing, verification,
or immutable-snapshot evidence fails closed.
