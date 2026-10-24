"""A disabled schedule must also block direct retention activation."""
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONDITION = ROOT / 'roles/ass_retention/files/aurora-ass-retention-hold.conf'


def test_hold_is_applied_before_any_retention_unit_or_timer_change():
    tasks = yaml.safe_load((ROOT / 'roles/ass_retention/tasks/main.yml').read_text())
    assert tasks[0]['ansible.builtin.import_tasks'] == 'hold.yml'
    hold = yaml.safe_load((ROOT / 'roles/ass_retention/tasks/hold.yml').read_text())
    create = next(task for task in hold if task.get('ansible.builtin.copy', {}).get('dest') == '/etc/aurora/ass-retention.hold')
    release = next(task for task in hold if task.get('ansible.builtin.file', {}).get('state') == 'absent')
    assert create['when'] == 'not ((ass_retention_enabled | bool) and (ass_retention_timer_enabled | bool))'
    assert release['when'] == '(ass_retention_enabled | bool) and (ass_retention_timer_enabled | bool)'
    assert all(set(task.get('ansible.builtin.systemd', {})) <= {'daemon_reload'} for task in hold)


@pytest.mark.skipif(shutil.which('systemd-analyze') is None, reason='requires systemd condition evaluator')
def test_rendered_service_condition_blocks_held_and_allows_explicit_release(tmp_path):
    hold = tmp_path / 'retention.hold'
    condition = next(line for line in CONDITION.read_text().splitlines() if line.startswith('ConditionPathExists='))
    condition = condition.replace('/etc/aurora/ass-retention.hold', str(hold))
    hold.write_text('operator hold')
    blocked = subprocess.run(['systemd-analyze', 'condition', condition], capture_output=True, text=True)
    assert blocked.returncode != 0, blocked.stdout + blocked.stderr
    hold.unlink()  # Simulates the explicitly enabled inventory path, never production pruning.
    allowed = subprocess.run(['systemd-analyze', 'condition', condition], capture_output=True, text=True)
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
