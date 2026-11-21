# Development experiments

I keep experimental products separate from operational data. Code on `main`
does not promote a scientific result. The ordinary development API may show
candidates; automatic app fallback uses the isolated operational mirror.
See [Production and development](PRODUCTION_DEVELOPMENT.md) and
[Releases](RELEASES.md).

## Forecast provider comparison

`aurora-ecmwf-provider-shadow.timer` compares legacy and Earthkit decoding of
the latest mirrored deterministic GRIB. It writes only
`/data/aurora/dev-products/power/ecmwf_provider_shadow.json`, shadow history
and a daily gate report. Review requires seven days and 50 clean comparisons;
the gate never changes the configured provider. Production stays on
`AURORA_ECMWF_PROVIDER=legacy` until parity and resource gates pass.

Advisory writers use `/data/aurora/dev-products/power` on development and
`/data/aurora/products/power` on production. They do not change mirrored forecast
products. Operating scenarios learn load evidence from the UAS MQTT mirror;
neither site issues PDU commands.

## Issue-driven forecast publication

When enabled, the development chain is:

1. Capture one archive-eligible deterministic issue as an immutable snapshot.
2. Generate ENS, then 240-hour planning inputs. Copy both into the same staging tree.
3. Check generation age, source-cycle age, 90-hour coverage and cycle compatibility.
4. Build scenarios/display from those copies, checksum the full bundle, remove
   write bits and publish its pointer. On failure, retain the previous generation.

The manifest binds all three forecast identities, upstream manifest digests and
artifact hashes into `sourceManifestDigest`. It includes display energy,
recommendation history and CL61 diagnostics. Learner/history state is seeded
only in staging from the verified previous complete bundle (legacy files on the
first run). Failed or unpublished runs cannot mutate legacy state. Each attempt
has its own status and preserves its first detailed failure; a new attempt clears
old errors before work starts.

One exclusive producer lock covers issue capture, all input generation and
publication. Cached re-anchors defer as a false systemd condition while it is
held. Otherwise they publish under that same lock. Manual ENS, planning and
publication services also take it; the cached runner calls its publisher
without recursively acquiring it. Independent planning, ENS, scenario, candidate
and display timers stay disabled while this chain owns the work.

Cached rows remain auditable with `independentCycle=false` and
`ForecastVerificationEligible=false`. They do not advance `latest-independent`
or enter paired skill/adaptive-learning evidence.

## Public activation

Keep `aurora_power_forecast_publication_active=false` for the machinery release.
`forecast-bundle/active` stays absent and the existing v10 display stays available.
After `current/generation.json` is a complete advisory-only bundle, explicitly
set the flag true and reapply the focused release. Only this state may report
`activation_pending` for a missing ready marker.

Ansible resolves and verifies one immutable generation, then links `active`
directly to it. It never exposes a legacy directory or a moving `current`
symlink as a validated target. Later publications advance both pointers;
activated readers resolve through `active` so they cannot mix generations.
The additive development mobile status can read validated `current` before
public activation. The standalone scenario writer has its own mutable path and
cannot write inside a bundle.

## Candidate evaluation

The v12 launcher accepts only checksum-verified deterministic/ENS inputs from
one `latest-independent` generation. It rejects cached, incomplete, mismatched
or modified bundles. Candidate forcing uses embedded site-level
`ECMWFSolarIrradiance`, without reopening the roughly 50 MB historical global GRIB.
Preflight start/failure is written atomically to candidate status and appended
to evaluation history. Its 1.5G memory limit, separate candidate tree and deferral
while model evaluation is active remain in place. It is not an operational
Power dependency.

CL61 remains diagnostic intent: `cl61ActuationEnabled=false`. Bundle status and
append-only diagnostic history cannot operate a PDU. Unchanged publication
signatures update service state without rewriting Zarr or duplicating issues;
the signature includes SOC/load anchors, mode, ECMWF cycle, calibration, battery
parameters and model version.

## Display performance

`aurora-dashboard-display-manifest.timer` inventories prewarmed plots,
quicklooks, WXcam thumbnails and videos every five minutes. This atomic bounded
manifest is input for future publishing; it does not publish raw data or move Zarrs.
The health probe also compares public response times and mirror age every five
minutes. A slower healthy endpoint does not fail availability.

Panel expires unused documents after one minute on development and two on
production. Both check every 15 seconds and keep a 24-hour session-token lifetime.
