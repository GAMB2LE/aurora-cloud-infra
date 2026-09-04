# Backups and Archive Services

`aurora-cloud-infra` is the authoritative repository for every service that
pulls edge data to the cloud, mirrors it to the GWS or object store, verifies
archive parity, publishes archive health, or coordinates retention.

## The backup model in one minute

AURORA protects prune-managed raw observations in three stages, with four
physical copies while a file remains on ASS:

1. the live file on ASS;
2. the operational raw mirror on the production cloud host; and
3. two independent long-term archive copies, one on the JASMIN GWS and one in
   the `gamb2le-o` object-store bucket.

The cloud mirror is used for processing and is also checked before deletion,
but it is not sufficient on its own. An ASS file older than seven days becomes
eligible only after path and size match across the cloud mirror, GWS, and
object store; source/cloud/GWS verification also matches mtime, and checksums
are compared wherever both inventories provide them. The edge helper rechecks
the permitted file's exact path, size, and mtime immediately before deletion.

These other copies serve different purposes and do **not** replace the two raw
archives:

| Copy | Purpose | Counts as raw retention evidence? |
| --- | --- | --- |
| Production cloud raw mirror | Live processing and the first checked copy | Yes, but only together with GWS and object storage |
| JASMIN GWS | Additive long-term archive | Yes |
| `gamb2le-o` object store | Independent additive long-term archive | Yes |
| Development/data-ocean mirror | Development availability and UI testing | No |
| Derived Zarrs, quicklooks, catalogs, and videos | Rebuildable or presentation products | No |
| Proxmox/PBS guest backups | Recovery of virtual machines and configuration | No; they are not science-data parity evidence |

“Additive” means that writers copy new or changed files but never propagate a
source deletion to either archive. GWS rsync has no delete option and the
object-store writers use `rclone copy`, not `sync`.

## Repository boundaries

| Responsibility | Repository |
| --- | --- |
| ASS and APS host configuration and restricted delete helper | `aurora-edge-infra` |
| Edge-to-cloud source sync, GWS mirror, object-store mirror, verification, retention orchestration, archive monitoring | `aurora-cloud-infra` |
| Read-only presentation of archive health | `aurora_cloud_dashboard` |

The dashboard must not install transfer, verification, or pruning services.
It consumes the backward-compatible `health-v2` payload at
`/data/aurora/internal/archive_status/health-v1.json`. The filename remains
stable for older clients; the schema fields identify the payload contract.
Its generic operations collector may copy contract metrics into presentation
snapshots for compatibility, but it must not SSH-probe the GWS, parse archive
manifests, inspect archive writer units, or infer prune readiness.

## Canonical stream catalog

`inventory/group_vars/aurora_cloud.yml` contains `aurora_archive_streams`.
Each entry defines the edge source, cloud raw root, archive-relative path,
retention duration, and whether pruning is allowed. Add a stream there before
adding service-specific logic.

Radar (`rpgfmcw94`) uses the same seven-day ASS policy as the other verified
raw streams after two consecutive clean GWS/object-store reports. APS power
remains non-prunable pending a separate source and archive policy review.

The prune-managed ASS streams are CL61, radar, HATPRO, Vaisala MET, ASFS
science, ASFS fast sonic, ASFS fast gas, PDU, WXcam HDR media, and AURORACam.
APS Power is archived but explicitly excluded from edge pruning. Files under
the bulk raw tree that are not in this catalog can still be copied by the raw
writers, but they cannot be deleted from an edge host by the retention system.

## Data flow

```text
ASS / APS live files
        |
        | source-sync services
        v
production cloud raw mirror ----> derived products
        |                              |
        | additive GWS writers         | additive product writers
        | additive object writers      v
        v                         GWS + object store
 JASMIN GWS + object store
        |
        +---- independent verification evidence ----+
                                                     v
                                        health-v2 contract
                                                     |
                                    raw-only exact signed permit
                                                     v
                              restricted ASS deletion helper
```

Writers never wait for verification and never delete destination data. In
particular, the GWS rsync wrapper deliberately omits every `--delete` mode so
source retention cannot propagate into the archive.
Verification observes writers; it does not enable or disable them. A failed or
slow verifier therefore cannot stop uploads, although it blocks pruning.

Fresh raw delivery has a dedicated exact-path lane. After a source-sync job
successfully lands files on the cloud host, it records their path, size, and
mtime in a durable SQLite queue. Every two minutes
`aurora-archive-dispatch.service` selects a bounded newest-first batch and
copies only those files to both GWS and object storage. Each destination is
tracked independently, so a successful GWS copy is retained while a failed
object-store copy retries, and vice versa. The full-tree writers remain enabled
as an independent historical backstop.

Dispatch receipts prove that a fast-lane command completed; they are delivery
telemetry, not retention evidence. Only complete independent inventories and
the signed retention gate can authorize edge deletion.

## What is copied and when it becomes evidence

Copy age and verification age are intentionally separate. A writer may upload
a fresh file as soon as it has stopped changing, while parity waits longer so
live products do not oscillate between “present” and “missing”.

| Job | Content | Object-store writer minimum age | Verification age | Archive destination |
| --- | --- | ---: | ---: | --- |
| `raw` | Complete production raw mirror, excluding temporary radar partials | 15 min | 6 h | GWS raw and object-store raw |
| `products` | Selected products and quicklooks, excluding the WXcam subtree | 20 min | 30 h | GWS products and object-store products |
| `products-wxcam` | WXcam catalog, daily videos, and thumbnails; not `wxcam.zarr` | 30 min | 30 h | GWS WXcam products and object-store WXcam products |
| `model-evaluation` | Campaign products, dereferencing approved symlinked inputs | 1 h | 6 h | GWS and object-store model-evaluation roots |
| `manifests` | GWS verification evidence, excluding operational logs | 5 min | 2 h | GWS internal and object-store manifest roots |

The generic `products` job includes the Menapia flight catalog, per-flight
JSON/PNG files, and dated/latest UAS science quicklooks. They remain rebuildable
presentation products and do not count as raw-retention evidence; there is no
parallel Menapia-specific archive job.

The raw six-hour horizon is strict: every settled raw file must be present and
matching in object storage. The independent GWS stream verifier also publishes
all-age counts for visibility and seven-day age-bounded counts for retention.
Only the age-bounded raw counts can authorize deletion.

For products, a file younger than 30 hours that is not yet present in object
storage appears as `pending_upload`. Pending is normal delivery lag and does
not count as a parity failure or reset the stable-parity streak. If a product
is still absent or mismatched after 30 hours, it becomes a real archive gap,
turns archive health red, and triggers the exact-path repair service. This is
why a newly created quicklook no longer causes a permanent archive alert while
an old missing product remains visible.

Common working material is outside verified scope: Git and virtualenv trees,
caches, locks, partial or temporary files, SQLite WAL/SHM files, operational
logs, and product directories named as backup/schema-backup Zarrs. Symlink
pointers are excluded unless a job explicitly uses `copy_links`; in that case
the target bytes are archived and verified as a regular restorable file.

## Normal cadence

Schedules are UTC and may include a small randomized delay. A timer activation
can be skipped or bounded while an earlier run holds its lock; backlog then
continues on the next activation.

| Activity | Configured cadence |
| --- | --- |
| GWS raw | Every 5 minutes |
| GWS core products | Every 10 minutes |
| GWS WXcam products | Every 30 minutes |
| GWS model evaluation | Every 30 minutes |
| GWS manifests | Every 10 minutes |
| GWS source/cloud/archive verifier | Every 10 minutes |
| Newest-first raw dispatch | Every 2 minutes after a 15-minute settle window |
| Object-store raw and core products | Every 30 minutes |
| Object-store WXcam products | Hourly |
| Object-store model evaluation | Daily at 17:00 |
| Object-store manifests | Hourly |
| Complete object/GWS inventory | Daily at 03:20 |
| Raw retention evidence refresh | 07:20, 11:20, 15:20, 19:20, and 23:20 |
| Archive-health publication | Every 2 minutes |
| ASS retention | Daily at 03:30, provided every gate passes |

Raw-retention evidence expires after eight hours. Derived-product evidence has
a separate 36-hour limit: one daily cadence plus the full inventory service's
bounded 12-hour runtime. This longer product clock cannot authorize raw
deletion; the retention coordinator independently rechecks the raw family's
eight-hour evidence floor immediately before issuing any permit. Expired
product evidence is reported as an amber product-verification issue without
revoking otherwise-current raw retention proof.

The dispatch queue avoids a recursive discovery scan in the critical path:
source-sync jobs provide the exact files that just arrived. It sends at most
5,000 files or 20 GiB per run, ordered by newest mtime, and keeps independent
GWS and object-store completion flags. A deployment bootstrap may seed a
recent lookback, but normal operation is event-driven by successful ingress.

The full object writers reserve a bounded newest-first slice and then a bounded
full-history slice. A large backlog therefore converges over multiple cycles
without preventing new observations from receiving priority.

The full-history raw writer interleaves two bounded phases on every activation:
the newest two days are copied first, then a full-history backfill slice runs.
This prevents a multi-terabyte backlog from starving current observations.
High-cardinality product, camera, and manifest writers use the same two-phase
pattern: a short newest-first slice followed by a bounded full-history slice.
Thus every timer cycle reconsiders newly published chunks while historical
gaps continue to converge instead of falling permanently outside a lookback.
Full product inventories list each source-present product family in smaller
parallel shards; they never depend on one unbounded recursive object-store
root listing. Each completed family is atomically checkpointed as an explicitly
incremental report while the remaining families continue. Reused families keep
their own original proof timestamps, so a checkpoint can refresh raw-retention
evidence without pretending that unfinished products were checked. A report is
labelled `full` only after every required family succeeds. If a later family
fails, the completed checkpoints remain valid instead of losing hours of work.
Production runs up to two independent top-level jobs concurrently while all
shard listings share the global four-process limit. Local inventory walks prune
excluded directories before descending, so
the verifier no longer scans the excluded 635-GB WXcam pixel Zarr or its own
history tree. The manifest job excludes immutable `history/` and operational
`logs/`; those files are audit storage, not science parity evidence.
Raw inventories use the same rule for every family, including the multi-terabyte
radar archive. Families are scheduled independently, radar is listed as bounded
year/month subtrees, and a global process semaphore limits nested listings to
the configured `object_store_inventory_process_limit` (4 in production). This
is deliberately below the cloud host's CPU count: JASMIN object-store listings
are network-bound and excessive parallel scans can trigger gateway timeouts.
Each listing has a two-hour outer process guard in production, leaving
headroom for valid high-cardinality CL61 product shards that can run beyond one
hour while continuing to return data. The five-attempt policy and 12-hour
service timeout keep repeated failures bounded. Retries use an exponential,
jittered backoff so a group of failed shards does not immediately overload the
gateway again while still detecting dead connections promptly.
Source-proven flat families such as CL61 use a recursive files-only object
listing instead of S3's slow delimiter-based directory emulation; this keeps
the comparison exact while avoiding a pathological prefix scan.
Model-evaluation campaign data has independent additive writers to both GWS
and object storage; it is not implicitly covered by the products job.
Symlinked runtime inputs are dereferenced by both writers and verified as
regular, restorable files under their campaign-relative paths.

WXCam's live `wxcam.zarr` is a mutable derived working store and is
intentionally excluded from both product archives. Its immutable raw HDR
imagery, catalog, daily videos, and hourly thumbnails remain covered.
Because the Zarr is reproducible from archived imagery, it is never accepted
as retention evidence. Production no longer appends to this redundant pixel
cache. Its guarded cleanup tool requires fresh green strict archive evidence,
stable object-store parity, zero raw gaps, zero GWS stream issues, and a
disabled appender before it can remove the directory.

When an inventory publishes exact missing or mismatched paths,
`aurora-object-store-repair.path` starts the catalog-driven repair service.
The repair service takes the inventory publisher's manifest lock before reading
`latest` and holds it through result publication, so a checkpoint trigger
cannot bind a repair result to a report superseded by the same inventory run.
It revalidates every settled source file, rejects paths outside the configured
source root, follows symlinks only for jobs explicitly marked `copy_links`,
orders candidates newest first, and performs only
exact `rclone copy --files-from-raw` operations. It never deletes or broadly
rewalks the archive to repair a known finite gap.
After a successful repair, the repair service records exactly which catalogue
families copied settled paths and starts a bounded incremental inventory for
those families. It requires two clean confirmations ten minutes apart and
records the consumed repair report, so a path trigger cannot repeat completed
work. Each confirmation must publish a new gap-free full-family report and be
accepted by the verification gate with the exact report SHA; a zero inventory
exit alone is insufficient. A transient inventory or gate-observation failure
is retried once. The recheck unit has a
36-hour outer bound because two complete checks of a high-cardinality family
can exceed the ordinary single-inventory runtime without being stalled.
Every successful full, incremental, or resumable-family checkpoint reevaluates
the verification gate, including checkpoints retained when a later family
fails. Scheduled retention is started immediately only when the independent
raw-retention domain reports `raw_retention_ready=true`; a derived-product
delay cannot block an otherwise current, strict dual-archive raw proof. The
trigger exits successfully and pruning remains paused for any raw-domain gap,
stale proof, or incomplete confirmation.
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

`aurora-archive-health.timer` publishes schema `health-v2` every two minutes at
the stable compatibility path. Alongside the legacy metrics it publishes
explicit `delivery`, `durability`, `verification`, and `retention` objects.
The contract contains:

- per-stream GWS missing and mismatch counts;
- per-stream coverage and retention readiness;
- per-job and aggregate object-store missing and mismatch counts;
- per-job `pending_upload` counts for files still inside their verification
  settle window;
- per-job and aggregate direct GWS missing and mismatch counts for raw,
  products, WXCam products, model evaluation, and manifests;
- every catalog stream's source-sync timer and service state, including the
  independent radar and AURORACam historical backfill lanes;
- raw, product, WXCam product, model-evaluation, and manifest GWS writer states;
- every GWS/object-store writer and verifier service, timer, repair path, and
  verification-gate path state;
- verification timestamps and the object-store clean streak.
- newest-first delivery queue depth and bytes, per-destination pending counts,
  oldest pending age, last result, and last successful delivery time;
- a human-readable `operator_status` with separate level, title, detail, and
  whether pruning is paused.

The operator status separates transfer lag, archive loss, background
verification, and permission to prune:

- **green** means no settled gap is proven, delivery is under 30 minutes old,
  and certified raw evidence is current. A routine audit or second retention
  confirmation is visible status, not an alert;
- **amber** means delivery is 30--120 minutes behind, a verifier login or
  listing failed, or certified evidence is overdue while no settled gap is
  proven; pruning remains paused when raw evidence is not current;
- **red** means a settled file is missing or mismatched, delivery is stalled
  for two hours, or there is no previously clean certified baseline. Evidence
  age alone is not evidence of archive loss.

Raw retention and derived-product durability have independent gate state. A
settled product gap remains a red product-archive problem, but cannot reset a
clean raw-retention gate.

Dashboard text uses destination names, file counts, current verification
activity, and the pruning consequence. Raw metric tokens remain available in
the contract for diagnostics but are not used as the operator-facing message.

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

Missing or stale verification must be treated as unsafe for pruning, but it is
not automatically evidence of lost data. When the previous complete report is
clean, measured gaps remain zero, and a healthy inventory is running (or a
remote listing timed out), the operator status is amber and says that
verification is running or delayed and pruning is paused. Confirmed missing or
mismatched settled files, a failed writer, or an unsafe last complete report is
red. Neither state stops additive writers.

The status terms have precise meanings:

| State | Meaning | Operator action |
| --- | --- | --- |
| `pending_upload` | File is newer than the job's verification horizon and is not yet required for parity | Observe only; this is not an archive failure |
| dispatch queue pending | Exact recently landed raw files still need one or both archive copies | Observe progress; investigate if the oldest item keeps aging or the worker fails |
| `missing` or `mismatch` | A settled file is absent or differs at a destination | Let exact repair run, then verify again |
| inventory `running` with a recent heartbeat | A complete sharded scan is still progressing | Wait; do not infer a stall from report age alone |
| previous report clean, verification delayed | Current proof is unavailable but there is no measured gap | Amber; pruning is paused until a complete audit succeeds |
| inventory heartbeat older than five minutes while running | Verifier is stalled | Investigate the inventory service and its current shard |
| `clean=true`, streak `1` | One clean report; this may be a bounded exact-repair recheck | Not yet stable parity |
| `stable_parity=true` | Two distinct clean observations, with at least one complete audit of every family in that gate domain | That object-store gate domain is satisfied |
| stream `prune_ready=true` | That raw stream has exact age-bounded GWS/cloud candidates | Necessary but not sufficient for deletion |

The `health-v1` producer is the only code allowed to turn archive evidence into
operator state. Browser, mobile API, reports, and notification code are
read-only consumers of that result. This includes source-sync systemd health:
dashboard collectors must not keep a second list of source-sync units or probe
those units directly.

A fresh product can be pending without hiding raw archive health. A settled
product gap remains a real global archive-health failure, so aggregate object
parity cannot be green while an older non-raw job still has a gap.
Complete product coverage is compositional: each retained family must have a
fresh `full_family` proof with zero settled gaps, while at least one product
family must actually be rechecked to advance the clean streak. This allows an
exact-repair recheck to close the affected family from a complete report
without pretending that merely republishing reused evidence is a new clean
observation.

## Safe convergence runbook

Run these on the cloud host. None of them enables pruning:

```bash
systemctl status aurora-mirror-verify.service
systemctl status aurora-archive-dispatch.service
systemctl status aurora-archive-dispatch.timer
systemctl status aurora-object-store-inventory.service
systemctl status aurora-object-store-repair.path
systemctl status aurora-object-store-verification-gate.path
systemctl is-enabled aurora-ass-retention.timer
cat /data/aurora/internal/archive_dispatch/status.json
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
   The parity snapshot trails live raw writers by six hours, derived products
   by 30 hours, model-evaluation data by six hours, and continuously changing
   verifier manifests by two hours. Fresh product files are published as
   `pending_upload` instead of gaps. These horizons remain well inside the
   seven-day edge-retention window. Raw GWS parity is read from one immutable
   verifier history directory and applies explicit age-bounded retention
   counters.
   `verification_settle_age` controls only this proof horizon. The independent
   `settle_age` used by additive copy writers remains 15 minutes for raw,
   20 minutes for products, 30 minutes for WXCam products, one hour for model
   evaluation, and five minutes for manifests; verification must never slow
   delivery of fresh data.
4. The repair path unit copies only the exact reported missing or mismatched
   paths. It must finish successfully before another inventory is started. It
   now refreshes every successfully repaired family automatically with the
   bounded incremental verifier. The manual equivalent is:

   ```bash
   sudo systemctl start aurora-object-store-inventory-incremental@model-evaluation.service
   ```

   The resulting report is complete for the named family and inherits the
   just-published evidence and original proof timestamps for unaffected
   families. It records the base report timestamp and SHA-256 and uses the same
   atomic publication and GWS/object comparisons as a full run. An old or deep
   merge chain cannot promote stale evidence: the gate evaluates each family's
   own `verified_at` timestamp. This lets a strict raw audit recover after an
   unrelated product failure without weakening the product gate. The global
   inventory lock prevents a full and incremental run from publishing at the
   same time.
5. Run a fresh full inventory. A report is clean only when every settled job has
   zero gaps and mismatches against both GWS and object storage and all
   retention-age raw GWS counters are zero. Pending product uploads do not
   count as gaps. If step 4 produced a clean incremental report, this full
   report is an independent confirmation. Stable parity always requires two
   distinct clean observations and at least one complete audit of every family
   in the relevant domain. The raw-retention domain additionally requires a
   current canonical GWS verifier summary with zero retention-age gaps or
   mismatches; derived-product staleness cannot satisfy or invalidate that raw
   proof.
7. Confirm `health-v1.json` is green and review both cloud and edge audit logs
   before any dry-run retention canary.

After deploying the fast lane, seed only recent arrivals and run one bounded
batch; the historical writers continue to own older convergence:

```bash
sudo -u aurora /usr/local/bin/aurora-archive-dispatch scan --job raw --lookback-hours 48
sudo systemctl start aurora-archive-dispatch.service
sudo -u aurora /usr/local/bin/aurora-archive-dispatch status
```

The rebuildable WXcam pixel cache is a separate cloud-capacity operation. Run
the command without `--apply` first. The tool refuses both modes unless the
appender is disabled and strict archive evidence is green; apply mode writes an
audit receipt before and after removal:

```bash
sudo /usr/local/sbin/aurora-cloud-cache-cleanup
sudo /usr/local/sbin/aurora-cloud-cache-cleanup --apply
```

The role defaults keep `aurora-ass-retention.timer` disabled and dry-run on.
The committed production host overrides enable the reviewed live policy; ASS
enables the restricted helper in live mode, while APS remains disabled and
non-prunable. A failed, interrupted, or partial inventory never replaces the
previous complete comparison and can never authorize deletion. Always check
the live unit and both audit logs rather than treating committed configuration
as proof of the current host state.

Retention permits are signed by the cloud coordinator using
`ass_retention_signing_private_key`; edge helpers trust only the corresponding
root-managed public key. Never commit either private-key material or an
unencrypted key variable. Each permit is also built from the immutable GWS
history directory named by its verification timestamp, never from a sequence
of reads against the moving `latest` pointer. Missing signing, verification,
or immutable-snapshot evidence fails closed.
