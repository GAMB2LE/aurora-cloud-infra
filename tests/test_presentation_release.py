"""A presentation release must preserve data routing and writer ownership."""
from pathlib import Path

from jinja2 import Environment, StrictUndefined
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'roles/presentation_release'


@pytest.mark.parametrize('development', [False, True])
def test_web_override_preserves_host_media_and_session_settings(development):
    revision = 'a' * 40
    domain = 'data-ocean.gamb2le.co.uk' if development else 'data.gamb2le.co.uk'
    wxcam = '/data/aurora/dev-products/science-continuity/wxcam/current/media' if development else '/data/aurora/products/wxcam'
    camera = '/data/aurora/dev-products/live-continuity/current/cameras' if development else '/project/aurora/raw/auroracam'
    values = dict(
        aurora_presentation_app_dir=f'/opt/aurora-presentations/{revision}',
        aurora_presentation_venv='/opt/aurora-presentations/runtime-20261024',
        aurora_panel_host='127.0.0.1', aurora_panel_port=5006,
        aurora_dashboard_allowed_origins=[domain, domain + ':443'],
        aurora_presentation_wxcam_media_dir=wxcam,
        aurora_presentation_auroracam_media_dir=camera,
        aurora_dashboard_session_check_ms=15000,
        aurora_dashboard_unused_session_lifetime_ms=60000 if development else 120000,
        aurora_dashboard_num_procs=1 if development else 2,
    )
    output = Environment(undefined=StrictUndefined).from_string(
        (ROLE / 'templates/aurora-dashboard.conf.j2').read_text()).render(**values)
    assert f'wxcam-media={wxcam}' in output
    assert f'auroracam-media={camera}' in output
    assert f'--allow-websocket-origin={domain}:443' in output
    assert f'--unused-session-lifetime={values["aurora_dashboard_unused_session_lifetime_ms"]}' in output
    assert 'ExecStart=\nExecStart=/opt/aurora-presentations/' in output
    # An additive EnvironmentFile leaves the serving-only continuity and
    # forecast overlays in force; resetting it would silently change inputs.
    assert 'EnvironmentFile=\n' not in output
    assert '/opt/aurora-cloud-dashboard' not in output


def test_release_is_limited_to_two_readers_and_requires_main_ancestry():
    playbook = yaml.safe_load((ROOT / 'playbooks/presentation_release.yml').read_text())[0]
    assert playbook['roles'] == ['presentation_release']
    tasks = (ROLE / 'tasks/main.yml').read_text()
    assert 'merge-base, --is-ancestor' in tasks and 'origin/main' in tasks
    assert 'aurora_presentation_app_dir != aurora_app_dir' in tasks
    assert 'aurora_presentation_venv != aurora_venv' in tasks
    assert 'git pull' not in tasks and 'pip install' not in tasks
    handlers = yaml.safe_load((ROLE / 'handlers/main.yml').read_text())
    assert handlers[0]['loop'] == ['aurora-dashboard.service', 'aurora-mobile-api.service']


def test_mirror_paths_do_not_repurpose_shared_writer_variables():
    mirror = ROOT / 'roles/operational_mirror'
    for relative in ('tasks/main.yml', 'templates/aurora-operational-mobile-api.service.j2',
                     'templates/aurora-operational-mobile-api.env.j2'):
        text = (mirror / relative).read_text()
        assert '{{ aurora_app_dir }}' not in text
        assert '{{ aurora_venv }}' not in text
    unit = (mirror / 'templates/aurora-operational-mobile-api.service.j2').read_text()
    assert 'WorkingDirectory={{ aurora_operational_mirror_app_dir }}' in unit
    assert 'ExecStart={{ aurora_operational_mirror_venv }}/bin/uvicorn' in unit


def test_primary_api_keeps_one_worker_without_a_host_override():
    output = Environment(undefined=StrictUndefined).from_string(
        (ROLE / 'templates/aurora-mobile-api.conf.j2').read_text()).render(
            aurora_presentation_app_dir='/opt/aurora-presentations/' + 'a' * 40,
            aurora_presentation_venv='/opt/aurora-presentations/runtime-20261024',
            aurora_mobile_api_host='127.0.0.1', aurora_mobile_api_port=8010,
        )
    assert '--port 8010 --workers 1' in output


def test_operations_provenance_keeps_the_existing_collector_command():
    tasks = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
    provenance = next(task for task in tasks if task.get('register') == 'presentation_provenance')
    content = provenance['ansible.builtin.copy']['content']
    assert 'EnvironmentFile=/etc/aurora-presentation.env' in content
    assert 'ExecStart' not in content and 'WorkingDirectory' not in content
    assert 'notify' not in provenance


@pytest.mark.parametrize('reader', ['aurora-dashboard', 'aurora-mobile-api'])
def test_canonical_observations_override_only_the_two_reader_environments(reader):
    template = (ROLE / f'templates/{reader}.conf.j2').read_text()
    # The environment is last, so legacy publication=true cannot select a
    # development current-observation product after the continuity overlay goes.
    environment_part = template.split('ExecStart=')[0]
    rendered = Environment(undefined=StrictUndefined).from_string(environment_part).render(
        aurora_presentation_app_dir='/opt/aurora-presentations/' + 'a' * 40,
        aurora_presentation_canonical_observations=True,
    )
    assert rendered.endswith('EnvironmentFile=/etc/aurora-canonical-observations.env\n')
    assert 'EnvironmentFile=\n' not in rendered


def test_canonical_observations_preserve_development_forecast_selection():
    settings = dict(line.split('=', 1) for line in
                    (ROLE / 'files/aurora-canonical-observations.env').read_text().splitlines()
                    if line and not line.startswith('#'))
    assert settings['AURORA_POWER_FORECAST_PUBLICATION_ACTIVE'] == 'false'
    assert settings['AURORA_POWER_FORECAST_ONLY_PUBLICATION_ACTIVE'] == 'true'
    assert settings['AURORA_OPERATIONAL_MIRROR'] == 'false'
    assert settings['POWER_CURRENT_DISPLAY_ZARR_PATH'].startswith('/data/aurora/products/')
    assert settings['BLESENSORS_ZARR_PATH'].startswith('/data/aurora/products/')
    assert 'POWER_FORECAST_DISPLAY_ZARR_PATH' not in settings
    assert 'AURORA_POWER_FORECAST_BUNDLE_MANIFEST_PATH' not in settings
    assert not any('INPUT_MODE' in key or 'INPUT_ROOT' in key for key in settings)


def test_retirement_is_limited_to_preserved_reader_overlays():
    tasks = yaml.safe_load((ROLE / 'tasks/canonical_observations.yml').read_text())
    preservation = next(task for task in tasks if task['name'].startswith('Preserve each'))
    retirement = next(task for task in tasks if task['name'].startswith('Retire only'))
    assert tasks.index(preservation) < tasks.index(retirement)
    assert 'item.stat.checksum' in preservation['ansible.builtin.copy']['dest']
    assert preservation['ansible.builtin.copy']['force'] is False
    assert preservation['when'] == 'item.stat.exists and not ansible_check_mode'
    preview = next(task for task in tasks if task['name'].startswith('Preview the recovery'))
    assert preview['when'] == 'item.stat.exists and ansible_check_mode'
    source = (ROLE / 'tasks/canonical_observations.yml').read_text()
    assert '95-uas-display-continuity.conf' in source
    assert '99-forecast-publication-only.conf' not in source
    assert 'aurora-live-continuity.service' not in source
    assert 'aurora-science-continuity' not in source
