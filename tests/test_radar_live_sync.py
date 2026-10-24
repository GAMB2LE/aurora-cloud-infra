"""Exercise the actual live wrapper with transport and filesystem guard stubs."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

from jinja2 import Environment, StrictUndefined
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def run_sync(tmp_path, *, recover=False, maximum=1000, failure=''):
    bins = tmp_path / 'bin'
    bins.mkdir()
    source = tmp_path / 'raw source'
    source.mkdir()
    # These names would expand an unquoted wildcard in the remote shell.
    (source / 'unexpected-home-match.LV1.NC').touch()
    state = tmp_path / 'checkpoint'
    original = str(int(time.time()) - 1000)
    state.write_text(original)
    log = tmp_path / 'calls'
    stub = '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
name=pathlib.Path(sys.argv[0]).name
with open(os.environ['CALL_LOG'],'a') as out: out.write(json.dumps([name,sys.argv[1:]])+'\\n')
if name == 'ssh':
    if os.environ['FAILURE']=='ssh': sys.exit(23)
    sys.exit(subprocess.run(['bash','-c',sys.argv[-1]]).returncode)
if name == 'find':
    assert sys.argv[sys.argv.index('-name')+1]=='*.LV1.NC', sys.argv
    sys.stdout.buffer.write(b'100\\tY2026/latest.LV1.NC\\0')
if name == 'rsync' and os.environ['FAILURE']=='rsync': sys.exit(23)
if name == 'guard' and os.environ['FAILURE']=='guard' and sys.argv[sys.argv.index('--max-file-bytes')+1]!='0': sys.exit(75)
'''
    for name in ('ssh', 'find', 'rsync', 'flock', 'guard'):
        p = bins / name
        p.write_text(stub)
        p.chmod(0o755)
    env = Environment(undefined=StrictUndefined)
    env.filters['quote'] = lambda value: shlex.quote(str(value))
    rendered = env.from_string((ROOT / 'roles/source_sync/templates/aurora-radar-sync.j2').read_text()).render(
        radar_source_user='aurora', radar_source_host='source', radar_source_port=22,
        radar_source_path=source, radar_live_source_pattern='*.LV1.NC', radar_source_destination=tmp_path,
        radar_source_state_file=state, radar_source_bootstrap_lookback_seconds=1800,
        radar_source_overlap_seconds=600, radar_source_mtime_grace_seconds=300,
        radar_source_required_mount=tmp_path, radar_backfill_free_reserve_bytes=8,
        radar_source_max_batch_bytes=maximum, aurora_service_user='aurora', aurora_state_root=tmp_path)
    rendered = rendered.replace('/usr/local/lib/aurora-radar-transfer-guard.py', str(bins / 'guard'))
    rendered = rendered.replace('/home/aurora/.ssh/known_hosts', str(tmp_path / 'known_hosts'))
    rendered = rendered.replace('/usr/local/bin/aurora-archive-dispatch', str(tmp_path / 'absent-dispatch'))
    script = tmp_path / 'sync'
    script.write_text(rendered)
    subprocess.run(['bash', '-n', str(script)], check=True)
    args = ['bash', str(script)]
    if recover:
        args += ['--recover-window', '1', '2']
    result = subprocess.run(args, env={**os.environ, 'PATH': str(bins)+os.pathsep+os.environ['PATH'], 'CALL_LOG': str(log), 'FAILURE': failure}, text=True, capture_output=True)
    calls = [json.loads(row) for row in log.read_text().splitlines()]
    return result, calls, state.read_text(), original


def test_literal_remote_pattern_capacity_and_successful_checkpoint(tmp_path):
    result, calls, state, original = run_sync(tmp_path)
    assert result.returncode == 0, result.stderr
    guards = [args for name, args in calls if name == 'guard']
    assert guards[1][guards[1].index('--max-file-bytes')+1] == '100'
    transfers = [args for name, args in calls if name == 'rsync']
    assert len(transfers) == 1
    assert any(arg.endswith('/files') for arg in transfers[0])
    assert state.strip() != original


def test_recovery_is_finite_and_preserves_live_checkpoint(tmp_path):
    result, calls, state, original = run_sync(tmp_path, recover=True)
    assert result.returncode == 0, result.stderr
    assert state == original
    assert 'normal live checkpoint unchanged' in result.stdout


@pytest.mark.parametrize('failure', ['ssh', 'rsync', 'guard'])
def test_failure_never_advances_checkpoint(tmp_path, failure):
    result, calls, state, original = run_sync(tmp_path, failure=failure)
    assert result.returncode != 0
    assert state == original
    if failure != 'rsync':
        assert not any(name == 'rsync' for name, _ in calls)


def test_oversized_window_holds_before_transfer(tmp_path):
    result, calls, state, original = run_sync(tmp_path, maximum=99)
    assert result.returncode != 0
    assert 'live batch hold' in result.stderr
    assert not any(name == 'rsync' for name, _ in calls)
    assert state == original


def test_live_freshness_filter_does_not_narrow_native_backfill_scope():
    values = yaml.safe_load((ROOT / 'inventory/group_vars/aurora_cloud.yml').read_text())
    assert values['radar_live_source_pattern'] == '*.LV1.NC'
    assert values['radar_source_pattern'] == '*'
    backfill = (ROOT / 'roles/source_sync/templates/aurora-radar-backfill.j2').read_text()
    assert 'source_pattern={{ radar_source_pattern | quote }}' in backfill
    assert 'radar_live_source_pattern' not in backfill
