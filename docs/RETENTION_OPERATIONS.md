# Guarded ASS and cloud radar retention

These are separate operations: ASS pruning recovers field capacity; cloud radar
retention recovers `/data` capacity. Both run only on the production coordinator.
The acceptance evidence for the November 2026 handover is retained under
`/var/lib/aurora-retention-return-20261121` and the runtime directories below.

## Managed schedules and scope

| Operation | Timer target | Daily UTC schedule | Workload bound |
| --- | --- | --- | --- |
| ASS source pruning | `aurora-ass-retention-window.service` | 03:30, up to 20 minutes jitter | 5,000 exact candidates; signed permits contain at most 500 paths |
| Cloud historical radar retention | `aurora-cloud-radar-retention.service` | 05:00, up to 10 minutes jitter | One reviewed day |

The cloud controller admits only `Y2026/M05/D02`, `D03`, `D04`, `D05`, `D24`
and `D26`. It preserves their original science paths through fully verified,
immutable, read-only compressed images and removes only the redundant flat
copies. This is not a policy for arbitrary dates or general product deletion.
The existing May 13 image and historical acceptance records remain unchanged.

WXcam source pruning is explicitly disabled in the production host policy:
the upstream sender recreates identical retired images. Preserve that exclusion
until sender-side behaviour is corrected and an actual bounded prune remains
retired through a subsequent push. Other eligible ASS streams remain scheduled.

## Inspect before changing anything

On the production cloud host:

```sh
sudo /usr/local/sbin/aurora-cloud-radar-retention-preview --max-days 1
systemctl list-timers --all aurora-ass-retention.timer aurora-cloud-radar-retention.timer
systemctl show aurora-cloud-radar-retention.service -p MainPID -p ActiveState -p Result -p ExecMainStatus
sudo journalctl -u aurora-cloud-radar-retention.service -n 30 --no-pager
df -h /data
```

The preview is read-only and never authorizes deletion. It reports exact source
versions, archive/image preparation still required, capacity and blockers. It
returns `busy` while the actual worker is live. An empty blocker list does not
replace the full image hash, fresh archive gates or locked cutover validation.

`Result=success` while a oneshot is still activating is not terminal success.
Require `MainPID=0`, inactive state, exit status zero, accepted receipts and
restoration evidence. A live admitted transfer or shared-lock holder must finish;
an observation timeout is not permission to restart it.

## Safety and evidence

ASS windows retain the legacy `/etc/aurora/ass-retention.hold` between runs.
They drain conflicting admissions, acquire the existing coordinator leases,
perform archive readbacks, release the hold only for a bounded signed batch,
match cloud and edge receipts, require every selected source path to be absent,
and read back surviving archive samples before restoring protections.
Later source files with changed versions are fresh science data and must survive.

Cloud retention verifies all 96 candidate payloads in both independent archives
and in the replacement image. Under a journalled maintenance window it drains
reviewed consumers, acquires the relocation lease, changes the persistent mount,
and requires the ordinary 640-file continuity and science-account layout checks
before unlinking exact unchanged duplicates. It records each unlink durably and
measures actual filesystem recovery. Remaining historical files stay online.
The 640-file continuity check compares current metadata against historical byte
proofs; it must not be described as a new hash of every historical payload.

| Evidence | Production path |
| --- | --- |
| ASS window pointer and results | `/var/lib/aurora-cloud/retention-windows/` |
| Signed cloud receipts | `/data/aurora/internal/retention/` |
| Independent edge audit | ASS: `/var/log/aurora-edge/retention.jsonl` |
| Cloud per-day results and unlink journals | `/var/lib/aurora-cloud/radar-product-retention/` |
| Cloud maintenance pointer and restoration records | `/var/lib/aurora-cloud/radar-retention-windows/` |
| Compressed-view registry | `/etc/aurora/radar-retention-compressed.json` |
| Images and complete byte-readback receipts | `/data/aurora/recovery/retention-images-20261121/` |

The permanent `stage-withdrawal.json` in the historical relocation state prevents
old staging/release commands from refilling retired flat copies. Do not remove
it, alter historical manifests, force gate readiness or delete an unfamiliar hold.

Expired raw evidence and safely restored pre-mutation admission deferrals exit
75 and retry after 15 minutes. Ordinary Python failures exit 1 and stop. An unsafe
mutation or failed restoration is not converted into a retryable admission wait.
The enabled restore services reconcile interrupted owned windows at boot; they
do not grant permission to resume an unverified mutation.

## Deployment and acceptance

Deploy the normal cloud policy from this checkout:

```sh
uv run ansible-playbook -i inventory/hosts.yml playbooks/radar_product_retention.yml --limit aurora-cloud
```

Do not pass `radar_product_retention_initial_catchup=true` during normal operation:
it installs the one-off initial trigger and two explicit pilot-day arguments.
ASS window configuration is managed by `playbooks/retention_window.yml`; its
stream exclusion policy is managed by `playbooks/retention_source_policy.yml`.
Both retain the existing ASFS storage guard and signed edge verification.

Return-to-service acceptance requires actual scheduled outcomes, matching
receipts, restored holds/admissions, measured cloud usage below the 80% warning
threshold with useful headroom, verified science views and archive retrieval,
and a subsequent ingestion/product cycle without retired-copy refill. Test passes,
timer enablement and a running PID alone do not prove this outcome.

Check public product timestamps and the storage alert independently. Report
unrelated recent radar archive discrepancies and the WXcam exclusion explicitly;
do not hide those alerts or equate storage recovery with globally clean archives.
