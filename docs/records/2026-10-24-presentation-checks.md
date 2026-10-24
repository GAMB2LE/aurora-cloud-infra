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

The separate primary Operations collector now uses source
`1a588513a6f715f2159d0f65c41b857f1ccca9d3`, tagged `prod-20261024.3` and reachable
from main after dashboard PRs 27–28. Those changes passed 711 tests plus 55
subtests and integrated CI. The focused collector release changed only its
working directory and command, retained both output paths and environment files,
and reloaded systemd for the existing timer. Its scheduled 07:41 and 07:42 runs
exited successfully. Both public HTTP bootstrap checks and their legacy aliases
reported ready; the 07:42 snapshot also contained APS/ASS BLE samples from 07:41.
Browser rendering is a separate check. The source still reports the reader's
actual serving revision rather than confusing it with the collector revision.

At 07:44 UTC, data-ocean's ordinary dashboard and API also served
`prod-20261024.3`. Their observations, camera media, BLE, Operations and prewarm
paths now read the production replicas. The four temporary reader overlays were
saved with matching checksums before retirement. All 12 anonymous API routes
passed and seven actual media files matched the primary. Browser checks rendered
the three station cameras, current Power charts and development forecast charts.
The forecast API still selected its immutable development generation
`20261024T073255Z-29e678eb`; its continuity input producers were not stopped.

At 07:47 UTC the primary and isolated operational mirror also served this same
release. Both focused playbooks completed without failures and their serving
manifest assertions passed. An independent public check found schema 3 on both,
with the primary marked authoritative and the mirror marked `live-mirror` and
`operationalMirror=true`. The mirror points browser users to JASMIN.

The reloaded JASMIN Power page rendered its plots and both enclosure samples from
07:45 UTC. Operations also rendered: it identified the clean `prod-20261024.3`
reader, reported all nine stream groups current and all three source hosts
reachable, and labelled both public HTTP bootstraps ready. Its remaining service
and archive warnings are retained; a rendered page does not resolve those jobs.

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

The dedicated mirror API was commissioned publicly by 07:32 UTC at
`https://data-ocean.gamb2le.co.uk/mobile/operational/v1`. It reports schema 3,
`dataRole=live-mirror`, `operationalMirror=true`, revision `222ce9ea1ca0` and the
JASMIN browser address. Its candidate evaluation endpoint returns 404. The
focused deployment validated nginx and changed only this reader and route.
All 12 checked anonymous routes returned 200, and the same seven actual media
files matched SHA-256 through the public primary and mirror routes. The Android
primary and mirror live contract tests both passed at 07:32 UTC, with no skips.

A second direct product read at 07:32 UTC found equal radar, HATPRO, meteorology,
ASFS logger/sonic/gas and PDU observations. CL61, Power and WXcam were about five
minutes behind JASMIN, consistent with the mirror schedule. This is measured
transfer lag, not a claim of simultaneous acquisition. The ordinary development
API and its continuity producers remain separate; forecast dependencies need to
be checked before those temporary inputs can be retired.

## DigitalOcean capacity

The approved data volume increase from 1,000 to 1,200 GiB completed at 07:28 UTC.
I then expanded ext4 and verified both `/data` and its `/project` bind. The block
device is 1,288,490,188,800 bytes; the filesystem reported 239,518,982,144 bytes
available, about 223 GiB, at 82% used. The additional 200 GiB adds the approved
$20/month. All three dashboard/API services remained active.

Writes to this filesystem were briefly frozen for the provider resize, with an
automatic thaw watchdog armed first. I thawed it as soon as the new device size
was visible, expanded the mounted filesystem and cancelled the watchdog. No
science observations were removed to obtain this space.

## Apps and source integration

| Repository | Completed source on main | Distribution |
| --- | --- | --- |
| Dashboard | PRs 25–28, integrated main `9f8624eab3c07de17c4bf1138f3485d7fef9469b` | All three readers serve the pinned `prod-20261024.3` source above. |
| iOS | PR 9, `8f0a66fa1501df1cc4c3eb4625c0ebac2e406430` | 55 unit and 9 iPhone UI tests plus both CI builds passed. No new TestFlight upload. |
| Android | PR 2, `ed2d0df3d66bbd4a457a4fa9987abc615075682d` | 45 JVM and 15 emulator tests, lint and builds passed. No signed beta upload. |
| Infrastructure | Recovery, isolated presentation, mirror transport, radar and BLE changes merged in PRs 59–70. | Only the focused deployments described in these records were applied. |

iOS signing is now verified through Xcode with the existing distribution identity;
the diagnostic export was not uploaded. A host compiler pipe stall prevented the
fresh local archive, so an exact-main unsigned CI archive is being prepared for
local signing as version 0.13.2, build 28. The existing Android upload key was
found in the established iCloud signing folder. Its Keychain approval and access
to the owning Play Console account are still needed. Historical beta invitations
confirm the existing Play channel. I have not replaced either app's signing identity. Original dirty
work and unfinished scientific experiments remain preserved separately.

## Remaining gates

- Finish the verified radar relocation before restoring complete native
  backfill. The current LV1 live lane is updating; that does not clear older raw
  delivery debt or fill the historical product gap.
- Complete archive verification and resolve its reported discrepancies. Mirror
  equality is not independent archive parity. Retention remains disabled.
- Restore Menapia flight-archive S3 access. The credential expired on 30 September
  and bucket listing returns AccessDenied. Current UAS MQTT telemetry is updating.
- Publish the final documentation status and complete native beta distribution
  after their prerequisites pass.

The [documentation update plan](https://gamb2le.pages.dev/modernization/primary-restoration-20261024/)
owns the cross-repository checklist. Current procedures remain in
[Production and development](../PRODUCTION_DEVELOPMENT.md),
[Releases](../RELEASES.md) and [Radar raw relocation](../RADAR_RAW_RELOCATION.md).
