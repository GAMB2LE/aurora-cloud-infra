# Dashboard and app checks, 24 October 2026

I restored JASMIN as the normal dashboard and kept data-ocean as development
plus a read-only production mirror. These are dated checks, not current health.

## Serving code and observations

The first isolated reader release was `prod-20261024.1` (`ac672ae`), followed by
the share-link fix in `.2` (`222ce9e`). By 07:47 UTC, primary, development and the
isolated operational mirror served `.3`
(`1a588513a6f715f2159d0f65c41b857f1ccca9d3`). Reader source and Python 3.12
runtimes were separate from the scientific writers. The separately released
primary collector reported the actual serving revision.

Browser checks rendered Overview, Power, Operations and camera views. The share
field used `/app?...` rather than Panel's websocket path. Operations retained
its service/archive warnings: rendering did not resolve those jobs.

Direct paired product reads at 07:19 and 07:32 UTC found matching observations
or measured mirror lag of roughly five minutes. Seven actual media files matched
SHA-256 through primary and mirror API routes. BLE has its own
[commissioning record](2026-10-24-blesensors-commissioning.md).

The operational mirror at `/mobile/operational/v1` reported schema 3,
`dataRole=live-mirror`, `operationalMirror=true` and the JASMIN browser URL.
Its 12 anonymous routes passed and candidate evaluation returned 404. Android's
primary/mirror live contract tests passed without skips. The ordinary development
API and forecast continuity producers remained separate.

At 07:54 the three DO readers were active on `.3`. Its rendered BLE label read
“SwitchBot via ASS - JASMIN mirror”, with APS/ASS samples at 07:51 UTC. Current
and forecast Power plots rendered after that label correction.

## Capacity and app distribution

The approved DO volume increase to 1,200 GiB and ext4 expansion completed at
07:28. `/data` and its `/project` bind were verified with about 223 GiB available.
The extra 200 GiB added the approved $20/month. No observations were removed.

| Repository | Source integrated | Distribution at this check |
| --- | --- | --- |
| Dashboard | PRs 25–28, main `9f8624e` | Three readers served the exact `.3` pin above. |
| iOS | PR 9, `8f0a66f` | Unit/UI tests and CI passed; no new TestFlight upload. |
| Android | PR 2, `ed2d0df` | JVM/emulator tests, lint and builds passed; no signed beta upload. |
| Infrastructure | PRs 59–70 | Only the focused deployments recorded here were applied. |

Existing signing identities and unfinished source work were preserved. iOS had
an archive/compiler blocker; Android still needed Keychain and owning Play
Console access. Signing tests did not establish beta distribution.

## Still open at this check

Native radar backfill, its historical product gap, independent archive parity
and retention gates remained open. Menapia bucket listing returned `AccessDenied`
after the 30 September credential expiry; UAS MQTT telemetry was still updating.
Documentation publication and native beta distribution required their own receipts.

The [full original record](https://github.com/GAMB2LE/aurora-cloud-infra/blob/7f25708/docs/records/2026-10-24-presentation-checks.md)
retains exact product times, release/test counts and local evidence paths. Use
[Releases](../RELEASES.md) and [Production and development](../PRODUCTION_DEVELOPMENT.md)
for current procedures.
