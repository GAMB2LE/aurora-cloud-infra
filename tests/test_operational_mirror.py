"""The automatic fallback API must not expose development forecast products."""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, meta
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'roles/operational_mirror'


def environment():
    renderer = Environment(undefined=StrictUndefined)
    content = renderer.from_string((ROLE / 'templates/aurora-operational-mobile-api.env.j2').read_text()).render(
        aurora_domain='data-ocean.gamb2le.co.uk', aurora_app_dir='/opt/aurora-cloud-dashboard',
        aurora_dev_live_mirror_recent_minutes=15,
    )
    return dict(line.split('=', 1) for line in content.splitlines() if line and not line.startswith('#'))


def test_operational_environment_never_inherits_experimental_power():
    config = environment()
    assert config['AURORA_OPERATIONAL_MIRROR'] == 'true'
    assert config['AURORA_MOBILE_API_AUTH_MODE'] == 'public_read_only'
    assert config['AURORA_DATA_MODE'] == 'live-mirror'
    for key in ('AURORA_POWER_CANDIDATE_API_ENABLED', 'AURORA_POWER_FORECAST_ORCHESTRATION_ENABLED',
                'AURORA_POWER_FORECAST_PUBLICATION_ACTIVE', 'AURORA_POWER_PAIRED_EVALUATION_ENABLED'):
        assert config[key] == 'false'
    for key, value in config.items():
        assert '/dev-products' not in value and '/dev-raw' not in value, (key, value)
        if key.startswith('POWER_'):
            assert value.startswith('/data/aurora/products/power/'), (key, value)
    assert 'AURORA_MOBILE_API_TOKEN_FILE' not in config
    assert config['AURORA_INTERACTIVE_PREWARM_DIR'] == '/data/aurora/products/dashboard/prewarm'


def test_service_cannot_read_development_paths_or_write_products():
    unit = (ROLE / 'templates/aurora-operational-mobile-api.service.j2').read_text()
    assert 'EnvironmentFile=/etc/aurora-operational-mobile-api.env' in unit
    assert '/etc/aurora-dashboard.env' not in unit
    assert 'InaccessiblePaths=-/data/aurora/dev-products -/project/aurora/dev-raw' in unit
    assert 'ProtectSystem=strict' in unit
    assert 'ReadWritePaths=/var/cache/aurora-operational-mobile-api' in unit
    assert 'RequiresMountsFor=/data /project' in unit


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('tls', [False, True])
def test_route_is_explicit_opt_in_and_preserves_standard_api(enabled, tls):
    renderer = Environment(loader=FileSystemLoader(ROOT / 'roles/nginx/templates'), undefined=StrictUndefined)
    renderer.filters['bool'] = bool
    template = renderer.get_template('aurora-dashboard.nginx.j2')
    variables = {name: 'test' for name in meta.find_undeclared_variables(renderer.parse(template.environment.loader.get_source(renderer, template.name)[0]))}
    variables.update(aurora_tls_available=tls, aurora_nginx_rate_limits=True, aurora_is_development=True,
                     aurora_operational_mirror_enabled=enabled, aurora_operational_mirror_port=8011,
                     aurora_mobile_api_host='127.0.0.1', aurora_mobile_api_port=8010)
    rendered = template.render(**variables)
    assert ('location ^~ /mobile/operational/v1/ {' in rendered) is enabled
    assert 'proxy_pass http://127.0.0.1:8010/;' in rendered
    if enabled:
        assert 'proxy_pass http://127.0.0.1:8011/;' in rendered
        assert 'proxy_pass http://127.0.0.1:8011/media/wxcam/thumb/;' in rendered


def test_focused_playbook_has_no_writer_or_source_release_roles():
    playbook = yaml.safe_load((ROOT / 'playbooks/operational_mirror.yml').read_text())[0]
    assert playbook['hosts'] == 'aurora-cloud-droplet'
    assert playbook['roles'] == ['operational_mirror']
    config = yaml.safe_load((ROOT / 'inventory/group_vars/aurora_cloud.yml').read_text())
    assert config['aurora_operational_mirror_enabled'] is False
    tasks = (ROLE / 'tasks/main.yml').read_text()
    assert 'operational_mirror_manifest.json.deployment.operationalMirror' in tasks
    assert "operational_mirror_manifest.json.deployment.dataRole == 'live-mirror'" in tasks
    assert 'operational_mirror_dirty.stdout | trim' in tasks
