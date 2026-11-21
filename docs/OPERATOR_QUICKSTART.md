# Check AURORA

I start with [Production](https://data.gamb2le.co.uk/app), open Operations and
follow the affected stream from its source to the displayed observation.
[Data-ocean](https://data-ocean.gamb2le.co.uk/app) is the development mirror.
An HTTP response, active process or recent transfer stamp is not enough.

## First checks

1. Record the UTC check time, affected instrument, newest sample and alert.
2. Check the page renders properly, then compare source, raw, product and
   archive times. A stale product can have a working dashboard.
3. Log in through your approved `aurora-cloud` SSH alias and read the named
   service, timer and recent journal. Do not restart it while finding the cause.

```bash
ssh aurora-cloud
systemctl is-active aurora-dashboard.service nginx.service
systemctl --failed --no-pager
systemctl list-timers 'aurora-*'
cat /data/aurora/products/ops_monitor/health/latest_report.md
```

| Observation | Check next |
| --- | --- |
| Source sample is old | Instrument and edge acquisition |
| Source is current, raw or product is old | Source sync, append or quicklook job |
| Product is current, an archive is behind | Delivery queue, settling window and independent verification |
| Data are current, page is broken | Dashboard, nginx and public endpoint |
| Development differs | Its mirror stage and `last_success.json`, not production writer timers |

Use [Data locations](DATA_LOCATIONS.md) and the source guides for exact paths.
Keep passwords, private keys and tokens in the approved credential store.

## Source access

Most ASS/APS syncs use Tailscale SSH through ordinary `ssh`, with private-key
authentication disabled. The source guides note any exception. A small access
check for ASS is:

```bash
sudo -u aurora ssh -o IdentityFile=none -o PubkeyAuthentication=no aurora@100.124.55.22 true
```

Recovery tunnels are a separate access path. Their listener or fingerprint
check does not prove a successful login or usable desktop. See
[Reverse tunnels](REVERSE_TUNNELS.md).

## Storage

Check actual mounts and quotas before restarting a job:

```bash
findmnt -T /project/aurora/raw
findmnt -T /data/aurora/products
df -hT /project/aurora/raw /data/aurora/products
df -i /project/aurora/raw /data/aurora/products
quota -s
```

Include the service account and project quota. A missing quota report is an
unverified limit. Shared mounts are not separate capacity. I keep the largest
job reservation plus seven days of measured net growth; I do not invent a rate
or delete observations to make a disk look healthy.

## Archives

```bash
sudo aurora-object-store-recovery status
systemctl status aurora-mirror-verify.service --no-pager
systemctl status aurora-archive-dispatch.service --no-pager
cat /data/aurora/internal/archive_status/health-v1.json
```

Recent radar files can be pending for 45 minutes settling plus 30 minutes
transfer time. A missing path alerts after that window. Mismatches and failed
or overdue verification still need attention. Other product files have their
own settling periods. See [Archives and alerts](ARCHIVE_SERVICES.md).

A transient retry with valid clean proof can continue normally. An expired
proof is a verification problem, not automatic evidence of lost data. Check
worker identity, heartbeat and counters before calling a scan stalled.
Do not start a duplicate collector, edit a checkpoint or clear an alert by hand.

ASS deletion needs an exact signed permit, fresh independent proof and the
edge helper's checks. A green card or `prune_ready=true` alone is insufficient.
An operator hold remains binding. APS Power is not prunable.

## Change or escalate

Send a short incident note with the UTC time, affected stage, newest source and
product samples, relevant unit/journal output and evidence paths. Say what was
observed and what remains unverified.

Use [Releases](RELEASES.md) for a reviewed, focused deployment and rollback.
Use [Failover](FAILOVER.md) for any writer handover. A plain template rollout
must not replace a storage-safety wrapper. Keep raw data, current checkpoints
and unrelated services intact.
