import json
import hashlib
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
import retry_admission as admission


@pytest.fixture
def sample(monkeypatch):
    expected = {'relative_path': 'Y2026/M11/D03/file.LV1', 'size': 10, 'mtime_ns': 1, 'sha256': 'a' * 64}
    value = {'mode': 'read-only', 'stage_verified': True, 'gws_verified': False,
             'object_verified': False, 'retention_authorized': False, 'eviction_authorized': False,
             'gws_absent': True, 'holds_ok': True, 'capacity_ok': True,
             'source_proof': {**expected, 'stable': True},
             'stage_proof': {'schema': 'radar-direct-gws-stage-verified-v1', 'size': 10,
                             'sha256': 'a' * 64, 'retention_authorized': False, 'eviction_authorized': False}}
    identity = {'unit': 'aurora-radar-readonly-admission-test.service', 'invocation_id': 'b' * 32, 'main_pid': 42}
    observation = json.dumps({'relative_path': expected['relative_path'], 'size': 10,
                              'mtime_seconds': 0, 'observed_at': 100}).encode()
    argv = ['/usr/bin/python3', '/safe/launcher.py', '--request', '/safe/request.json',
            '--observation', '/safe/observation.json', '--observation-sha256', hashlib.sha256(observation).hexdigest()]
    props = {'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'exited', 'Result': 'success',
             'ExecMainStatus': '0', 'ExecMainPID': '42', 'InvocationID': 'b' * 32, 'ControlGroup': '',
             'ExecStart': '{ path=/usr/bin/python3 ; argv[]=' + shlex.join(argv) + ' ; }'}
    monkeypatch.setattr(admission.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(admission.retry_observer, '_query', lambda unit: props.copy())
    monkeypatch.setattr(admission.retry_observer, '_pids', lambda group: [])
    monkeypatch.setattr(admission, '_launcher_hash', lambda path: 'c' * 64)
    monkeypatch.setattr(admission, '_read_root_owned', lambda path: observation)
    monkeypatch.setattr(admission.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=json.dumps(value).encode()))
    return identity, argv, expected, value, props


def test_accepts_only_exact_successful_admission(sample):
    identity, argv, expected, _, _ = sample
    proof = admission.accepted(identity, argv, '/safe/launcher.py', 'c' * 64, expected, now=100)
    assert proof['source_proof']['sha256'] == expected['sha256']
    with pytest.raises(ValueError, match='identity'):
        admission.accepted(identity, argv + ['--apply'], '/safe/launcher.py', 'c' * 64, expected, now=100)
    with pytest.raises(ValueError, match='stale'):
        admission.accepted(identity, argv, '/safe/launcher.py', 'c' * 64, expected, now=701)


def test_changed_worker_or_duplicate_proof_is_rejected(sample, monkeypatch):
    identity, argv, expected, value, props = sample
    props['InvocationID'] = 'd' * 32
    with pytest.raises(ValueError, match='recorded successful'):
        admission.accepted(identity, argv, '/safe/launcher.py', 'c' * 64, expected, now=100)
    props['InvocationID'] = 'b' * 32
    raw = json.dumps(value).encode()
    monkeypatch.setattr(admission.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=raw + b'\n' + raw))
    with pytest.raises(ValueError, match='ambiguous'):
        admission.accepted(identity, argv, '/safe/launcher.py', 'c' * 64, expected, now=100)
