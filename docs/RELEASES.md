# Releases from main

I keep completed code on `main` and deploy exact reviewed pins. A Git push does
not update a host. Record infrastructure, presentation, scientific-writer and
app revisions separately; they can differ.

Before release, preserve dirty files and deployed-only fixes on named branches.
Integrate from current `origin/main`, pass the merged checks and never force-push.
Hosts must have a clean checkout. Keep experiments separate from promotion.

## Choose the release

| Change | Focused playbook | Boundary |
| --- | --- | --- |
| Isolated dashboard/API readers | `presentation_release.yml` | Separate checkout/runtime; preserves scientific writers. |
| Operations snapshot code | `operations_collector_release.yml` | Collector command only; next scheduled run. |
| Operational fallback API | `operational_mirror.yml` | Dedicated development reader and nginx route. |
| Shared dashboard/API source | `dashboard_code_release.yml` | Exact source change, existing runtime, both readers restart. |
| Mobile API source only | `mobile_api_code_release.yml` | Exact source change, existing runtime, API restart only. |
| Dashboard service set | `dashboard_release.yml` | Code/runtime release, without source/storage/network roles. |
| Complete dashboard runtime | `dashboard_runtime_release.yml` | Includes source sync, nginx and mirror units; excludes archives/retention. |
| Host baseline | `site.yml` | Separate provisioning change. |
| Archive services | `archive_services.yml` | Independent archive release; inspect installed guards first. |

Run `uv run ansible-playbook playbooks/<name>.yml --limit <host> --check --diff`
with the reviewed variables file first. Apply the same command without check/diff
after reviewing its scope. Check mode is a preview, not candidate acceptance.
A missing package can make a broad provisioning preview fail before later tasks.

## Isolated presentation release

The shared `/opt/aurora-cloud-dashboard` also supplies scheduled science code.
Changing it affects the next writer run. Shared code releases therefore require
`aurora_shared_writer_source_change_reviewed=true`.

For readers only, stage a clean checkout at
`/opt/aurora-presentations/<full-commit>` with refreshed `origin/main`. Install
its pinned Python 3.12+ runtime separately. Keep the scientific checkout and
environment intact. Supply:

```yaml
aurora_presentation_revision: <40-character commit reachable from main>
aurora_presentation_venv: /opt/aurora-presentations/runtime-<release-date>
```

Record effective units and old paths before applying. The focused playbook
changes reader `WorkingDirectory`/`ExecStart`, checks the API revision and retains
environment/sandbox overrides. `zz-presentation-release.conf` takes precedence
over older command overrides. `aurora-presentation.env` records reader provenance
and disables bytecode writes; it does not set forecast-generator provenance.
The existing collector reads it on its next scheduled run.

A host serving direct-field media also needs explicit
`aurora_presentation_wxcam_media_dir` and
`aurora_presentation_auroracam_media_dir`. Environment overlays cannot establish
nginx's static roots. The operational mirror uses its own application/runtime
variables; never repoint shared writer variables to change a reader.

For rollback, restore the saved reader/collector drop-ins and presentation
environment. On the first isolated release, remove only the two reader drop-ins
and collector `zz-presentation-provenance.conf`. Reload systemd and restart the
two readers; check the next collector snapshot. Retain checkouts and runtimes
until rollback is no longer needed.

## Return development readers to canonical observations

First pass the operational mirror's observation/media checks. Then set
`aurora_presentation_canonical_observations=true` and use static roots
`/data/aurora/products/wxcam` and `/project/aurora/raw/auroracam`.

The focused release checksum-backs up four continuity reader overlays under
`/etc/aurora-retired-reader-overrides/20261024/` before retiring them.
`/etc/aurora-canonical-observations.env` selects replicated Power, BLE, Operations
and prewarm paths. Full forecast publication stays disabled; forecast-only
publication stays enabled. Development forecast/ENS/learning continuity producers
continue until a separately tested input handoff.

To reverse routing, restore the four exact saved overlays to their reader
filenames, clear the canonical flag, restore previous static roots and reapply
the focused release. Verify both public readers. Keep all continuity data.

## Source-only reader fixes

These releases need a clean checkout at the expected SHA, the configured origin,
an existing runtime and a healthy local API. Review the complete candidate diff,
including every writer using a shared checkout. They do not install dependencies,
service units, nginx, timers or data roles.

For the mobile API, supply:

```yaml
aurora_mobile_api_expected_revision: <40-character current host SHA>
aurora_mobile_api_target_revision: <40-character candidate SHA>
aurora_app_version: <same development SHA, or annotated prod-YYYYMMDD.N tag>
```

The dashboard/API source playbook uses
`aurora_code_release_expected_revision` and
`aurora_code_release_target_revision` instead. It restarts both readers and
updates only forecast source-revision metadata in the existing environment.
The mobile-only path restarts just the API when its SHA changes.

Production requires the remote annotated tag to resolve to the candidate.
Check mode observes the current host and predicts Git changes; it does not fetch,
compile or test the candidate. Git diff output is suppressed because Ansible
would fetch during that preview. Review the patch in the source repository.
A normal release compiles with the existing runtime and checks loopback health.
Public APIs, plots and browser interactions still need separate verification.
Record any inventory pin change explicitly.

When hosts intentionally differ, base each fix on its current pin. Test the exact
production candidate in a temporary loopback-only development process before
tagging; a hotfix must not silently promote unrelated experiments.

## Promotion and acceptance

1. Integrate on `main`, pass checks and record the full commit.
2. Deploy that exact candidate to data-ocean. Check the rendered app, development
   banner, source label, observation times, media and per-stage mirror lag.
3. Create an annotated `prod-YYYYMMDD.N` tag on that validated commit.
4. Deploy the exact tag to JASMIN. Check both public readers and active streams.

A healthy response alone does not prove a rendered UI or fresh observations.
Compare source, raw, product and API times across two scheduled cycles. Confirm
one authoritative writer, correct effective units, expected timers and no new
unexplained failures. Preserve old failed states for diagnosis. Independent
archive acceptance and retention remain separate gates.

Before writer cutover, save both hosts' Git state, active/failed units, all
`aurora-*` timers, stream frontiers, archive health and mounts/capacity. JASMIN
must reach ASS, APS and GWS. Follow [Failover](FAILOVER.md) for ownership changes.

## Development rollback and baseline evidence

Protected release snapshots are under
`/var/lib/aurora-release-snapshots/<UTC timestamp>`. Verify their manifests before
restoring only affected files. Keep them off Git and public documentation.
Application rollback never restores or deletes observations.

For a complete development rollback:

```bash
uv run ansible-playbook playbooks/dev_dashboard_rollback.yml \
  -e aurora_rollback_snapshot_name=YYYYMMDDTHHMMSSZ \
  -e aurora_rollback_dashboard_revision=<40-character previous commit>
```

This refuses production and requires the checksum-bound snapshot's exact
`source_commit`. It quiesces named Power writers/evaluators and readers, restores
configuration and pinned runtime/source, then restores captured timer states.
Product data stay in place.

Before the first v12 machinery release, explicitly freeze development's v10
baseline while publication is off:

```bash
uv run ansible-playbook playbooks/power_v10_baseline_snapshot.yml \
  --limit aurora-cloud-droplet --check --diff \
  -e aurora_power_baseline_snapshot_name=v10-pre-v12-YYYYMMDD
```

After review, apply without check/diff. The immutable name cannot be reused.
The playbook temporarily quiesces derived writers, holds the producer lock,
copies a literal allowlist and restores exact prior unit states on every exit.
It SHA-checks both sides, publishes atomically and makes the snapshot root-owned
and non-writable. It retains allowlisted non-secret settings and installed unit
checksums; artifact provenance remains the authority for generating code.
Raw observations, mirrored inputs, global GRIB/cache trees, candidates and
staging are excluded. This evidence snapshot is not an automatic rollback source.

## Changes needing approval

Get explicit approval for writer/host ownership changes, source-sync logic,
Zarr/SQLite/schema migrations, production data paths, nginx/DNS/certificates,
alert recipients/routing, secrets/SSH/Tailscale/credentials, or destructive
cleanup and data rollback. Low-risk reader-only fixes can be released after
staging checks. Existing authorization for a named change still applies.

Use the smallest rollback that covers the release, preserve its evidence and
verify the result independently. A completed push, passing test or installed
unit does not establish deployment.
