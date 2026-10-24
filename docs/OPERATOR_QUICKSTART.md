# AURORA Operator Quickstart

This short runbook is for approved GAMB2LE operators checking whether the
AURORA data pipeline and dashboard are healthy. It is deliberately read-only:
use it to establish the affected stage and collect evidence before proposing a
change.

## Access

Request an individual, approved SSH account and the `aurora-cloud` and
`aurora-cloud-droplet` SSH aliases from the project access owner. Do not put
passwords, private keys, API tokens, or shared account details in this
repository or in a ticket. Store and rotate those only through the approved
access-controlled credential process.

The two public dashboards are:

| Site | URL | Purpose |
| --- | --- | --- |
| Production | <https://data.gamb2le.co.uk/app> | The stable service and authoritative writer. |
| Development | <https://data-ocean.gamb2le.co.uk/app> | A development view of mirrored production data. |

Start with Production. Development must show its development banner and must
not be treated as an independent production writer.

## Five-minute health check

1. Open **Operations Dashboard** on Production. Record the Overall state,
   affected stream, source/local/GWS freshness, storage warnings, and any
   public-endpoint or mirror-lag warning.
2. Check the public page renders fully. A bare `Bokeh Application` page is an
   application failure, even if the HTTP request succeeded.
3. If Operations indicates a problem, log in to the production host:

   ```bash
   ssh aurora-cloud
   sudo systemctl is-active aurora-dashboard.service nginx.service
   sudo systemctl --failed --no-pager
   sudo systemctl list-timers --all 'aurora-*'
   sudo -u aurora git -C /opt/aurora-cloud-dashboard status --short --branch
   cat /data/aurora/products/ops_monitor/health/latest_report.md
   ```

   These commands only read status. Do not restart services, enable timers, or
   edit data while establishing the cause.

4. When Development is implicated, check its mirror rather than its production
   writer timers:

   ```bash
   ssh aurora-cloud-droplet
   sudo systemctl is-active aurora-dashboard.service nginx.service
   sudo systemctl list-timers --all 'aurora-dev-live-pull-*.timer'
   sudo journalctl -u 'aurora-dev-live-pull@*.service' --since '30 minutes ago' --no-pager
   cat /data/aurora/internal/dev-live-mirror/last_success.json
   ```

## Check storage before restarting jobs

On each named host, these commands only read the actual backing filesystem:

```bash
findmnt -T /project/aurora/raw
findmnt -T /data/aurora/products
df -hT /project/aurora/raw /data/aurora/products
df -i /project/aurora/raw /data/aurora/products
quota -s
```

Check the service account's quota and any Ceph/project quota as well. A missing
`quota` command or a blank report is not proof that there is no quota. Record
that limit as unverified until the storage-specific report is checked. Shared
mounts must not be counted as separate free capacity. Check binds with `findmnt`
before treating a relocated cache as safe.

I keep enough space for the largest relevant job reservation plus seven days
of measured net growth. Record free bytes at consistent UTC intervals and use
the recent sustained growth, including pending catch-up. If that record is
missing, measure it and keep the storage gate open; do not invent a rate.

Remove only verified disposable material. A relocation needs a closed data
set, copied bytes, checksum verification and a tested access path before the
old copy can be removed. Preserve raw observations and archive evidence. Do not
run a recursive delete to make a full filesystem look healthy.

## Read archive status correctly

The development mirror is not a backup gate. Archive authority comes from the
production `health-v1` contract, which combines cloud, GWS, and object-store
evidence. For a red archive card, collect these read-only checks on production:

```bash
sudo systemctl status aurora-mirror-verify.service --no-pager
sudo systemctl status aurora-object-store-inventory.service --no-pager
sudo systemctl status aurora-object-store-repair.service --no-pager
sudo systemctl status aurora-ass-retention.timer --no-pager
cat /data/aurora/internal/object_store_manifests/progress.json
cat /var/lib/aurora-cloud/object-store-verification-gate/state.json
cat /data/aurora/internal/archive_status/health-v1.json
```

Interpret the result before escalating:

| Signal | Meaning |
| --- | --- |
| `pending_upload` | A product is still inside its 30-hour settle window; it is visible but is not a parity failure. |
| settled `missing` or `mismatch` | A real archive gap; exact repair and a new inventory are required. |
| inventory running with heartbeat under five minutes old | Slow but progressing verification. |
| running with heartbeat over five minutes old | Stalled verifier. |
| clean streak `1` | First clean report; stable parity still needs a second distinct report. An exact-repair incremental recheck may supply this first result. |
| `stable_parity=true` | Two distinct clean observations are present and at least one completely verifies every family in that gate domain. |
| `prune_ready=true` | One raw stream has exact age-bounded candidates; this alone is not deletion permission. |

Do not start pruning, edit manifests, or clear an alert manually. Writers and
exact repair continue independently; retention fails closed until fresh
evidence passes.

## Locate the failed stage

Use the Operations Dashboard and `latest_report.md` to distinguish these
states; do not treat every red card as a single outage.

| Observation | Likely stage | Next read-only evidence |
| --- | --- | --- |
| Source is stale, but the dashboard is otherwise healthy | Instrument or edge-host acquisition | Compare the newest source timestamp with the local raw mirror. |
| Source is current, but local data/product is stale | Source sync, append, or quicklook job | Check the named stream's timer/service and its recent journal. |
| Local product is current, but GWS or object storage is behind | Archive transfer, settle window, or verification | Distinguish pending from a settled gap; do not prune source data. |
| Data are current but the page is incomplete or slow | Dashboard, nginx, or Panel | Check the public endpoint card, `aurora-dashboard.service`, and nginx. |
| Development differs from Production | Development mirror | Check the staged `aurora-dev-live-pull-*.timer` units, per-stage JSON, and `last_success.json`. |

Configured paths and stream-specific commands are in [Data Locations](DATA_LOCATIONS.md)
and the **Source Syncs** pages. Use the Operations Dashboard as the source of
truth for *current* freshness and service health; repository documentation is
the intended operating contract.

## Release or recover

Use [Releases](RELEASES.md) for tested commits on `main`, exact deployment pins
and rollback. Presentation recovery may use a separate checkout so scheduled
scientific writers keep their existing code. Use [Failover](FAILOVER.md) for
outage reconciliation and any deliberate ownership transfer.

The iOS and Android repositories own their build/signing procedures. Record
build, upload, processing, tester availability and device checks separately;
an upload alone is not a usable beta release.

## Escalate with useful evidence

Post or send a concise incident note containing:

- UTC time checked and the Production/Development URL.
- Affected stream and whether source, local raw, product, GWS, or the public
  dashboard is stale.
- Output of the relevant `systemctl` and `journalctl` checks, plus the report
  path/timestamp.
- Whether data continue to arrive and whether any customer-facing dashboard
  view is affected.

Only an approved maintainer may change writer timers, source-sync behaviour,
networking, credentials, raw/product paths, alert routing, or delete/roll back
data. Follow [Production and Development](PRODUCTION_DEVELOPMENT.md) for
release and recovery policy.
