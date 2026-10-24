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
