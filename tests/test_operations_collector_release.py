"""A collector release must preserve its commissioned scope and timer."""
from pathlib import Path
import re
import shlex

from jinja2 import Environment, StrictUndefined
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'roles/operations_collector_release'


def release_values():
    revision = 'a' * 40
    return dict(
        aurora_site_env='production', aurora_is_primary=True,
        aurora_operations_collector_revision=revision,
        aurora_operations_collector_app_dir='/opt/aurora-presentations/' + revision,
        aurora_operations_collector_venv='/opt/aurora-presentations/runtime-20261024',
        aurora_app_dir='/opt/aurora-cloud-dashboard', aurora_venv='/opt/aurora-venv',
        aurora_service_user='aurora', aurora_service_group='aurora',
        aurora_paths={'raw_ops_monitor': '/project/example/raw/ops_monitor',
                      'products_ops_monitor_health': '/data/example/products/ops_monitor/health'},
    )


def test_collector_override_changes_only_working_directory_and_command():
    values = release_values()
    environment = Environment(undefined=StrictUndefined)
    override = environment.from_string((ROLE / 'templates/aurora-ops-monitor-collect.conf.j2').read_text()).render(**values)
    base = environment.from_string((ROOT / 'roles/dashboard_services/templates/aurora-ops-monitor-collect.service.j2').read_text()).render(**values)
    settings = [line.split('=', 1) for line in override.splitlines() if '=' in line and not line.startswith('#')]
    assert [key for key, _ in settings] == ['WorkingDirectory', 'ExecStart', 'ExecStart']
    assert settings[1][1] == ''
    new_command = shlex.split(settings[2][1])
    old_command = shlex.split(next(line.split('=', 1)[1] for line in base.splitlines() if line.startswith('ExecStart=')))
    assert new_command[0] == values['aurora_operations_collector_venv'] + '/bin/python'
    assert new_command[1:] == old_command[1:]
    assert new_command[1:] == ['collect_operations_snapshot.py', '--output-root',
                              values['aurora_paths']['raw_ops_monitor'], '--health-output-root',
                              values['aurora_paths']['products_ops_monitor_health']]


@pytest.mark.parametrize('changes', [
    {},
    {'aurora_site_env': 'development'},
    {'aurora_is_primary': False},
    {'aurora_operations_collector_revision': 'abc123'},
    {'aurora_operations_collector_app_dir': '/opt/aurora-cloud-dashboard'},
    {'aurora_operations_collector_venv': '/opt/aurora-venv'},
    {'aurora_operations_collector_venv': '/opt/aurora-presentations/runtime/../writer'},
    {'aurora_operations_collector_venv': '/opt/aurora-presentations/..'},
])
def test_release_requires_primary_full_revision_and_separate_paths(changes):
    tasks = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
    conditions = tasks[0]['ansible.builtin.assert']['that']
    environment = Environment(undefined=StrictUndefined)
    environment.tests['match'] = lambda value, pattern: re.match(pattern, value) is not None
    environment.filters['bool'] = bool
    values = release_values() | changes
    accepted = all(environment.compile_expression(condition)(**values) for condition in conditions)
    assert accepted is (not changes)


def test_validation_precedes_override_and_import_does_not_collect():
    tasks = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
    override_index = next(index for index, task in enumerate(tasks) if 'ansible.builtin.template' in task)
    commands = [task['ansible.builtin.command'] for task in tasks[:override_index] if 'ansible.builtin.command' in task]
    arguments = [command['argv'] for command in commands]
    assert any(argv[-2:] == ['rev-parse', 'HEAD'] for argv in arguments)
    assert any(argv[-2:] == ['status', '--porcelain'] for argv in arguments)
    assert any('merge-base' in argv and '--is-ancestor' in argv and argv[-1] == 'origin/main' for argv in arguments)
    runtime = next(argv for argv in arguments if '-c' in argv)
    code = runtime[runtime.index('-c') + 1]
    compile(code, '<collector-import-check>', 'exec')
    assert 'import collect_operations_snapshot, enclosure_sensors' in code
    assert 'sys.prefix' in code and '__file__' in code
    assert 'build_snapshot(' not in code and '.main(' not in code
    assert any('LoadState' in ' '.join(argv) for argv in arguments)
    assertions = [task['ansible.builtin.assert']['that'] for task in tasks[:override_index] if 'ansible.builtin.assert' in task]
    assert ['operations_collector_revision.stdout == aurora_operations_collector_revision',
            "operations_collector_dirty.stdout | trim == ''"] in assertions


def test_only_collector_dropin_and_release_record_are_written():
    tasks = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
    destinations = {task[module]['dest'] for task in tasks
                    for module in ('ansible.builtin.copy', 'ansible.builtin.template') if module in task}
    assert destinations == {
        '/etc/systemd/system/aurora-ops-monitor-collect.service.d/zz-operations-collector-release.conf',
        '/etc/aurora-operations-collector-release.json',
    }
    handlers = yaml.safe_load((ROLE / 'handlers/main.yml').read_text())
    service_actions = [task['ansible.builtin.systemd'] for task in tasks + handlers if 'ansible.builtin.systemd' in task]
    assert service_actions == [{'daemon_reload': True}]
    assert all(task[module].get('backup') is True for task in tasks
               for module in ('ansible.builtin.copy', 'ansible.builtin.template') if module in task)
    playbook = yaml.safe_load((ROOT / 'playbooks/operations_collector_release.yml').read_text())[0]
    assert playbook['roles'] == ['operations_collector_release']
