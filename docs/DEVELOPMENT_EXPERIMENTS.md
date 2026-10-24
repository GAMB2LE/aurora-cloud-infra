# Development experiments

These are isolated development workflows. They do not become operational
products because their code is on `main`, and they are never automatic app
fallback data. See [Production and Development](PRODUCTION_DEVELOPMENT.md)
for ownership and [Releases](RELEASES.md) for promotion.

Development also runs `aurora-ecmwf-provider-shadow.timer`. This performs a
read-only comparison of the latest mirrored deterministic ECMWF GRIB with the
legacy and Earthkit decoders. It writes only
`/data/aurora/dev-products/power/ecmwf_provider_shadow.json`, appends a shadow
history, and writes a daily promotion-gate report. The gate requires seven days
and 50 clean comparisons before it can be reviewed; it never changes the
configured provider. `aurora-dashboard-health-probe.timer` also compares public
development and production response times every five minutes and records mirror
age. A development-versus-production latency delta is an observation only: it
does not fail the availability probe when both endpoints are healthy.

Development may run advisory forecast writers only in
`/data/aurora/dev-products/power`; it never modifies the mirrored production
forecast products. Production runs the same advisory jobs in
`/data/aurora/products/power`. The operating-scenario service reads the UAS
MQTT mirror so it can learn effective-tier load evidence. Neither environment
issues PDU commands.

When issue-driven Power orchestration is enabled on development, one successful
archive-eligible deterministic issue captures an immutable issue snapshot and
starts the ENS and 240-hour planning inputs in that order. Only after both
inputs have been copied into the same staging tree do the deterministic,
planning, and ENS snapshots pass explicit generation-age, source-cycle-age,
90-hour coverage, and cycle-compatibility checks. Scenario, display, and
checksum work then reads only those staged copies. The complete manifest binds
the three forecast identities, upstream source-manifest digests, and artifact
hashes into one `sourceManifestDigest` and
also covers display-energy, the generation-local recommendation archive, and
CL61 diagnostic products. The scenario generator seeds recommendation history
and adaptive load-model state from the checksum-verified previous complete
bundle (or the legacy files for the first generation) only into staging;
neither an unpublished run nor a failed bundle mutates the legacy development
learner state or recommendation archive. Completed issue and generation trees
have their write bits removed before a pointer can expose them. A failed stage
retains the prior generation and records a fail-closed status with the first
detailed failure preserved within a distinct attempt ID; the next deterministic
or cached attempt clears that failure before doing work, so stale errors cannot
hide a later run's cause.

The full-cycle and cached deterministic writers share one exclusive file lock.
The dev full-cycle runner retains that lock across deterministic issue capture,
ENS generation, planning generation, and complete staged publication. This
prevents the three-hour timer or a cached re-anchor from replacing the issue or
mutable source products between stages. A cached run that finds the full runner
busy is deferred as a false systemd condition and cannot trigger a publisher
against stale output. Otherwise its generation and complete staged publication
occur while it still holds the lock. Manually invoked ENS, planning, and
full-publication services also take the same lock, while the cached path calls
the publisher directly under its existing lock and never recursively acquires
it.
Cached re-anchors are labelled `independentCycle=false` and never advance the
`latest-independent` pointer used by candidate evidence. Cached rows remain in
the forecast archive with
`ForecastVerificationEligible=false`, so operational re-anchor behavior is
auditable without entering paired skill or adaptive-learning evidence. The
independent planning, ENS, scenario, candidate, and display timers are disabled
on development while this chain is active.

Public activation is deliberately two phase. The initial machinery release
keeps `aurora_power_forecast_publication_active: false`, leaves
`forecast-bundle/active` absent, and continues serving the established dev
display without adding a candidate warning to the legacy v10 payload. Once
`current/generation.json` is a real complete advisory-only bundle, set that
inventory switch to `true` and re-run the focused release. Only then can a
missing ready marker report `activation_pending`. Ansible validates the
manifest from one resolved generation and links `active` directly to that same
immutable generation; no legacy directory or moving `current` symlink is ever
presented as the validated activation target. Subsequent successful
publications advance `current` and
`active` to the same generation. `current` is internal publisher state; every
activated consumer resolves its products through the single validated `active`
pointer so readers cannot mix generations during pointer updates.

The v12 evaluator receives the deterministic and ENS snapshots from one
checksum-verified `latest-independent` generation. Its launcher rejects cached,
incomplete, mismatched, or modified bundles before invoking the candidate.
Candidate forcing comes from the site-level `ECMWFSolarIrradiance` embedded in
the immutable deterministic artifact; it never reopens or retains the roughly
50 MB global-grid GRIB named by historical provenance.
The launcher atomically records preflight start/failure in the candidate status
and append-only evaluation history, so an identity, digest, or anchor rejection
is visible to operations and iOS rather than only in the systemd journal.
It remains constrained by `MemoryMax=1.5G`, defers while the separate AURORA
model-evaluation service is active, writes only its candidate tree, and is not a
dependency of the operational Power bundle. CL61 output remains diagnostic
shadow intent with `cl61ActuationEnabled=false`; intent and current status live
only inside each validated bundle. The separate CL61 history remains an
append-only diagnostic evidence stream and cannot actuate or update a PDU.
The additive development mobile endpoint reads the status from the validated
`current` bundle even while public forecast-bundle activation remains off; the
standalone scenario writer keeps a separate mutable output path and can never
write into that immutable bundle.
Production remains on `AURORA_ECMWF_PROVIDER=legacy` until the parity and
resource gates pass.

Forecast and scenario services use semantic publication signatures. A run with
unchanged SOC/load anchors, mode, ECMWF cycle, solar calibration, battery
parameters, and model version updates service state without rewriting the
public Zarr or adding a duplicate verification issue.

## Development-only display performance work

The development host may run bounded presentation experiments that do not
change raw data, product Zarrs, source synchronization, or writer ownership.

`aurora-dashboard-display-manifest.timer` inventories prewarmed Plotly JSON,
quicklooks, WXcam thumbnails, and daily videos every five minutes. The manifest
is an atomic, bounded input for a future CDN or object-store publishing job; it
does not publish raw data and does not move any Zarr store.

Development expires unused Panel documents after one minute so backgrounded
phone sessions stop retaining full server-side documents promptly. Production
uses two minutes. Both hosts check every 15 seconds and retain a 24-hour
session-token lifetime.
