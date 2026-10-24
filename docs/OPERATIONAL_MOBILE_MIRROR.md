# Operational mobile mirror

The production apps can use `/mobile/operational/v1` on data-ocean during a
JASMIN connection or server failure. I keep this separate from `/mobile/v1`,
which serves the development dashboard's products and may include candidate
forecasts.

## What it runs

`aurora-operational-mobile-api.service` runs the reviewed presentation code on
loopback port 8011, with one worker. It reads an independent allowlisted
`/etc/aurora-operational-mobile-api.env`, not the shared dashboard environment.
Power, UAS, science and camera paths point to production replicas. Candidate,
paired evaluation, shadow and forecast-bundle activation are disabled. Systemd
makes development raw/product trees inaccessible to this process and permits
writes only to `/var/cache/aurora-operational-mobile-api`.

The API uses `public_read_only` with no administration token. Its manifest must
report schema 3, `dataRole: live-mirror`, `operationalMirror: true`, the reviewed
revision and the JASMIN browser URL. The flag identifies the process contract;
it does not establish that observations are fresh.

## Commission it after the data gates

First verify two scheduled cycles for the required JASMIN streams and their
mirrors, plus observation timestamps in Power, Operations, UAS and camera
products. Keep emergency continuity and experiments intact until their
replacement data have passed those checks.

Deploy the reviewed presentation revision separately with its own Python 3.12
or newer virtual environment. Supply that exact `aurora_operational_mirror_venv`; the mirror role
does not upgrade or reuse the scientific writer environment. This playbook does not
clone code, install dependencies, change writer timers or touch archives.
Create a private release variables file with the full deployed commit:

```yaml
aurora_operational_mirror_enabled: true
aurora_operational_mirror_expected_revision: <40-character presentation commit>
aurora_operational_mirror_app_dir: /opt/aurora-presentations/<same commit>
aurora_operational_mirror_venv: /opt/aurora-presentations/<same commit>/venv
```

From the infrastructure checkout, review the existing nginx site diff:

```bash
uv run ansible-playbook playbooks/operational_mirror.yml   --limit aurora-cloud-droplet -e @/path/to/mirror-release.yml --check --diff
```

Then apply that same command without `--check --diff`. This changes only the
operational API environment/unit/cache and the existing nginx site. The role
checks the clean source revision, starts the API, validates its manifest, and
then reloads nginx after `nginx -t`. Keep the template backup if the nginx
validation fails; repair or restore that site before any later reload.

The nginx template is shared with other deployments. After commissioning,
record the enabled flag and presentation path in reviewed host inventory so a
later deployment does not remove the route or select the shared writer checkout.
Use only these dedicated mirror variables; changing the shared `aurora_app_dir`
or `aurora_venv` would also change future scientific writer deployments. Do not
set the enabled flag before the evidence gates pass.

## Verify the result

Read-only checks from the controller and development host:

```bash
curl --fail --silent --show-error https://data-ocean.gamb2le.co.uk/mobile/operational/v1/manifest
ssh aurora-cloud-droplet 'systemctl status aurora-operational-mobile-api.service --no-pager'
ssh aurora-cloud-droplet 'systemctl cat aurora-operational-mobile-api.service'
```

Check rendered native views as well as JSON. Verify the source label and age,
relative media URLs through this route, cached/offline state and return to
JASMIN. Test an anonymous request; never forward production credentials to the
mirror. Recheck the service after every presentation code change. Existing
code-release playbooks restart the original web/API services only.

To withdraw this endpoint, set its inventory flag false and run the focused
playbook. It stops this API and removes its nginx route. It does not remove
observations, alter the ordinary development API, or transfer writer ownership.
