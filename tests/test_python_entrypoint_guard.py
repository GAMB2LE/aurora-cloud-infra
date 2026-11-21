"""A template rollout must not replace an installed storage wrapper."""
import importlib.util
import os
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location('entrypoint_guard', ROOT / 'scripts/check_python_entrypoint.py')
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


def test_missing_or_plain_entrypoint_can_be_installed(tmp_path):
    path = tmp_path / 'program'
    GUARD.require_plain_entrypoint(path, owner=os.getuid())
    path.write_text('# run_python_original is only mentioned in a comment\nprint("ready")\n')
    GUARD.require_plain_entrypoint(path, owner=os.getuid())


@pytest.mark.parametrize('call', [
    'run_python_original("program", "pin", [])',
    'guard.run_python_original("program", "pin", [])',
    'runpy.run_path("guard.py")["run_python_original"]("program", "pin", [])',
])
def test_all_supported_wrapper_calls_require_a_pinned_release(tmp_path, call):
    path = tmp_path / 'program';path.write_text(call + '\n')
    with pytest.raises(ValueError, match='pinned-payload release'):
        GUARD.require_plain_entrypoint(path, owner=os.getuid())
    assert path.read_text() == call + '\n'


def test_link_or_unprotected_program_is_not_replaced(tmp_path):
    path = tmp_path / 'program';path.write_text('print("ready")\n')
    link = tmp_path / 'link';link.symlink_to(path)
    with pytest.raises(OSError): GUARD.require_plain_entrypoint(link, owner=os.getuid())
    path.chmod(0o666)
    with pytest.raises(ValueError): GUARD.require_plain_entrypoint(path, owner=os.getuid())
    path.chmod(0o600)
    with pytest.raises(ValueError): GUARD.require_plain_entrypoint(path, owner=os.getuid() + 1)


def test_special_file_is_refused_without_waiting_for_a_writer(tmp_path):
    path = tmp_path / 'program'
    os.mkfifo(path)
    with pytest.raises(ValueError, match='protected, root-owned program'):
        GUARD.require_plain_entrypoint(path, owner=os.getuid())


@pytest.mark.parametrize('role,install', [('gws_sync', 'Install mirror verification script'),
                                        ('operations_monitor', 'Install archive health collector')])
def test_guard_precedes_template_install_even_in_check_mode(role, install):
    tasks = yaml.safe_load((ROOT / 'roles' / role / 'tasks/main.yml').read_text())
    index = next(i for i,t in enumerate(tasks) if t['name'] == install)
    guard = tasks[index-1]
    assert 'check_python_entrypoint.py' in str(guard['ansible.builtin.command'])
    assert guard['check_mode'] is False
    assert guard['changed_when'] is False
    if role == 'operations_monitor': assert 'wxcam_reconcile' in guard['tags']
