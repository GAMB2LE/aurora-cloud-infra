# AURORA Cloud Failover

Failover is a manual operational change. It moves authoritative writer,
archive, alert, and retention ownership; it is not the same as using the live
development mirror.

The normal roles are:

| Site | Normal role |
| --- | --- |
| `data.gamb2le.co.uk` on JASMIN | Production, authoritative writers, archive services, alerts, and retention |
| `data-ocean.gamb2le.co.uk` on DigitalOcean | Development dashboard, isolated experiments, and staged live mirror |

Use [Production and Development](PRODUCTION_DEVELOPMENT.md) for routine releases.
Use this runbook only when Production cannot provide the required service and
an approved maintainer has explicitly authorized a role change.

## Safety rules

- Never run normal raw/product writers on both hosts.
- Never run archive retention from two hosts.
- Stop retention before changing writer ownership. Keep it stopped until the
  promoted host has produced two complete, distinct clean archive reports.
- Preserve raw data and products. Application rollback must not delete or roll
  back data.
- Do not treat the development mirror as archive evidence or deletion proof.
- Do not change DNS, certificates, host roles, writer timers, archive
  credentials, or alert routing without explicit approval.

## Choose the smallest recovery

1. **Application-only recovery:** deploy the previous approved `prod-*` tag on
   JASMIN and restart only the dashboard service. Writer ownership does not
   change.
2. **Read-only continuity:** keep data-ocean in Development and direct users to
   its mirrored dashboard while Production is repaired. Writer ownership does
   not change.
3. **Full promotion:** move authoritative processing to data-ocean only when
   Production will be unavailable long enough to justify a controlled cutover.

Prefer the first or second option. A public page being unavailable does not by
itself require moving data writers.

## Capture the pre-change state

Record these items for both hosts before a full promotion:

- UTC time, incident reason, and approving operator
- deployed Git revision and dirty status
- active and failed services
- all AURORA timers
- newest raw and product timestamps for every active stream
- development per-stage mirror results and dashboard mirror stamp
- archive-health contract and retention state
- current DNS and certificate endpoints

Read-only commands:

```bash
sudo systemctl --failed --no-pager
sudo systemctl list-timers --all 'aurora-*'
sudo -u aurora git -C /opt/aurora-cloud-dashboard status --short --branch
sudo -u aurora git -C /opt/aurora-cloud-dashboard describe --tags --always --dirty
cat /data/aurora/internal/archive_status/health-v1.json
```

On Development, also record:

```bash
sudo systemctl list-timers --all 'aurora-dev-live-pull-*.timer'
ls -1 /var/lib/aurora-cloud/dev-live-mirror/*.json
cat /data/aurora/internal/dev-live-mirror/last_success.json
```

Save the inventory, timer lists, and Git state with the incident record before
making a change.

## Full-promotion gates

Do not start the cutover until all of these are true:

- the latest required production data have reached Development, or the known
  gap is documented and accepted;
- Development can reach the ASS and APS source hosts;
- Development has enough storage for continued acquisition;
- the exact approved dashboard and infrastructure revisions are clean;
- archive credentials and destinations have been tested without enabling
  retention;
- alert ownership and public endpoint behavior are agreed;
- a rollback owner and decision point are recorded.

## Full-promotion sequence

1. Disable retention on the current Production writer.
2. Pause Production source, append, quicklook, forecast, Operations, alert, and
   archive writer timers.
3. Run and verify the final staged mirror pulls to Development while JASMIN is
   still reachable.
4. Confirm sorted, readable products and compare latest timestamps for all
   active streams.
5. Commit the reviewed inventory change that makes data-ocean Production. Run
   the relevant Ansible playbook in check mode and review its complete diff.
6. Apply the approved role change. Enable source/product writers, Operations,
   alerts, and archive writers on only the promoted host. Leave retention off.
7. Change DNS or public routing only if the main production hostname must move.
   Verify nginx, certificates, websocket origins, and both public URLs.
8. Verify new raw files, appended products, quicklooks, forecasts, and
   Operations snapshots advance on the promoted host.
9. Run fresh GWS and object-store verification. Enable retention only after two
   complete, distinct clean reports establish stable parity.

If a gate fails, stop. Restore the previous role configuration rather than
continuing with partial ownership.

## Verification after promotion

Confirm all of the following:

- one host, and only one host, owns normal writer timers;
- the promoted checkout is clean and matches the approved revision;
- no unexpected systemd units are failed;
- source and product timestamps advance for powered-on instruments;
- intentionally powered-off instruments are reported as expected pauses;
- Power, AURORACam, WXcam, UAS, and Operations render from current data;
- archive writers advance and retention remains disabled until its gate passes;
- the native iOS app reaches the intended production API endpoint.

## Failback to JASMIN

Failback is the same controlled ownership transfer in reverse:

1. repair and validate JASMIN without enabling writers;
2. mirror all data acquired during failover back to JASMIN;
3. compare stream frontiers and product integrity;
4. stop retention and all writers on the promoted droplet;
5. apply the reviewed inventory change restoring JASMIN Production;
6. enable writers and archives only on JASMIN;
7. return data-ocean to Development and enable its staged mirror timers;
8. re-establish two clean archive reports before re-enabling retention.

Do not simply restart old JASMIN timers. It must first be caught up and made the
single authoritative writer.

## Historical record

The repository history contains the July 2026 JASMIN shutdown and temporary
droplet-writer notes. They explain past decisions but are not current commands.
This page and the current inventory are the operational contract.
