# Dashboard and app checks, 24 October 2026

I am restoring JASMIN as the normal dashboard and keeping data-ocean as development
and a read-only production mirror. This record separates what is serving from
what is merged, staged or still waiting for an external dependency.

## Serving release

The first isolated presentation release, `prod-20261024.1`, is
`ac672ae6d835d05461b2fcb8c209151aa8ace2de`. It is reachable from dashboard `main`
and passed 691 tests plus 45 subtests and the release workflow. It was serving
the development dashboard/API at 07:00 UTC and JASMIN at 07:05 UTC.

Both readers use their own immutable source directory and Python 3.12 runtime.
The original scientific writer checkout and environment remain pinned. The
Operations collector reads the presentation provenance without changing its
command, timer or writer environment. Its 07:16 snapshot reported the clean
serving revision `ac672ae`.

The browser checks covered the rendered Overview, Power, Operations and camera
views. JASMIN showed all three active station cameras and live APS/ASS enclosure
readings. A separate share-link defect was found during that check: Panel's
websocket path had leaked into copied URLs. Dashboard PR 26 fixes the page URL;
its 694 tests plus 55 subtests and integrated CI passed before merge.

By 07:24 UTC both public readers served the follow-up `prod-20261024.2`,
`222ce9ea1ca0eec611435418df15f4ac18a1e332`. Each clean checkout was checked against
`origin/main` and imported with the existing isolated runtime before deployment.
The focused playbook changed only reader paths and provenance; its post-release
manifest checks passed on both hosts. The development camera share field now
contains `/app?...`, with the camera and selected time retained.

## Observations and mirror evidence

At 07:17 UTC all nine primary Operations stream groups reported current
collection. I also read the actual product time coordinates on each host; the
mirrored Operations snapshot alone cannot establish that a product arrived.
The following paired read was made at approximately 07:19 UTC.

| Product | JASMIN latest observation, UTC | Mirror latest observation, UTC |
| --- | --- | --- |
| CL61 | 24 October 07:10:40 | 24 October 07:10:40 |
| Radar | 24 October 06:59:52 | 24 October 06:59:52 |
| HATPRO | 24 October 06:44:55 | 24 October 06:44:55 |
| Meteorology | 24 October 07:16:20 | 24 October 07:11:48 |
| ASFS logger | 24 October 07:00:00 | 24 October 07:00:00 |
| Fast sonic | 24 October 06:30:00 | 24 October 06:30:00 |
| Fast gas | 24 October 07:00:00 | 24 October 07:00:00 |
| Power | 24 October 07:15:49 | 24 October 07:15:49 |
| PDU | 24 October 07:17:20 | 24 October 07:10:47 |
| WXcam catalogue | 24 October 07:10:39 | 24 October 07:10:39 |

The short differences are visible transfer lag, not missing readings to fill.
Two media checks fetched seven actual files through the API routes: three
station-camera originals, one preview, both WXcam thumbnails and a HATPRO
quicklook. Each returned an image and matched SHA-256 on both hosts. The second
check included both cameras' 24 October 06:30 thumbnails. BLE has its own
[commissioning and byte-comparison record](2026-10-24-blesensors-commissioning.md).

The dedicated mirror API is staged on loopback port 8011. It reports schema 3,
`dataRole=live-mirror`, `operationalMirror=true` and the JASMIN browser address.
Its candidate evaluation endpoint returns 404. Its public nginx route has not
been commissioned. Development continuity remains available while that gate is
held.

## Apps and source integration

| Repository | Completed source on main | Distribution |
| --- | --- | --- |
| Dashboard | PR 25, then share-link PR 26 | Pinned serving release above; subsequent rollout recorded separately. |
| iOS | PR 9, `8f0a66fa1501df1cc4c3eb4625c0ebac2e406430` | 55 unit and 9 iPhone UI tests plus both CI builds passed. No new TestFlight upload. |
| Android | PR 2, `ed2d0df3d66bbd4a457a4fa9987abc615075682d` | 45 JVM and 15 emulator tests, lint and builds passed. No signed beta upload. |
| Infrastructure | Recovery, isolated presentation, mirror transport, radar and BLE changes merged in PRs 59–70. | Only the focused deployments described in these records were applied. |

iOS distribution needs the existing Apple account restored in Xcode and a usable
distribution signing identity. A local compiler pipe stall also prevented the
final archive attempt. Android has no configured release signing keys; Play app
and tester access remain unverified. I have not replaced either app's signing identity. Original dirty
work and unfinished scientific experiments remain preserved separately.

## Remaining gates

- Increase the existing DigitalOcean volume from 1,000 to the approved 1,200 GiB,
  then verify both block-device and filesystem capacity. Approval is recorded;
  account sign-in is still needed. The observed 26–27 GiB free does not cover the
  measured seven-day growth allowance.
- Finish the verified radar relocation before restoring complete native
  backfill. The current LV1 live lane is updating; that does not clear older raw
  delivery debt or fill the historical product gap.
- Complete archive verification and resolve its reported discrepancies. Mirror
  equality is not independent archive parity. Retention remains disabled.
- Correct the old HTTP monitor's size-based false alarm. A valid Panel bootstrap
  is about 9.6 kB and renders through its websocket; HTTP bootstrap readiness and
  rendered browser validation need separate wording.
- Restore Menapia flight-archive S3 access. The credential expired on 30 September
  and bucket listing returns AccessDenied. Current UAS MQTT telemetry is updating.
- Publish the final documentation status and complete native beta distribution
  after their prerequisites pass.

The [documentation update plan](https://gamb2le.pages.dev/modernization/primary-restoration-20261024/)
owns the cross-repository checklist. Current procedures remain in
[Production and development](../PRODUCTION_DEVELOPMENT.md),
[Releases](../RELEASES.md) and [Radar raw relocation](../RADAR_RAW_RELOCATION.md).
