import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
import finalized_queue_driver as driver


def test_durable_worker_identity_and_unresolved_attempt(tmp_path, monkeypatch):
    out = tmp_path / 'preparation'; out.mkdir()
    expected = {'relative_path': 'a', 'size': 2, 'mtime_ns': 3, 'sha256': 'b'}
    operation = '20261027-gws-only-20261114t010203z-0'
    (out / 'request.json').write_text(json.dumps({'operation_id': operation, 'finalized_stage': {'expected': expected}}))
    (out / 'admission-evidence.json').write_text(json.dumps({'unit': 'aurora-finalized-admission-20261114t010203z.service',
        'accepted': True, 'invocation_id': 'f'*32, 'retention_authorized': False, 'eviction_authorized': False}))
    monkeypatch.setattr(driver, 'BASE', tmp_path)
    monkeypatch.setattr(driver, 'candidate_identity', lambda path: expected)
    monkeypatch.setattr(driver, 'finalized_argv', lambda *args: [])
    monkeypatch.setattr(driver, 'admission', lambda *args: {'accepted': True, 'invocation_id': 'f'*32})
    prep = {'out': str(out), 'operation': operation}
    instance = driver.FinalizedGwsDriver(prep)
    intent = instance.reserve(expected, 'gws-verified')
    op = {'id': 'c'*32, 'target': 'gws-verified', 'expected': expected, 'intent': intent}
    assert instance.observe(op) is None
    calls = []
    monkeypatch.setattr(driver, 'launch', lambda unit, path: calls.append(unit) or
                        {'unit': unit, 'invocation_id': 'd'*32, 'main_pid': 123})
    instance.apply(op)
    assert len(calls) == 1
    monkeypatch.setattr(driver, 'state', lambda unit: {'InvocationID': 'd'*32, 'ExecMainPID': '123',
                                                       'ActiveState': 'active', 'SubState': 'running'})
    assert instance.observe(op) is None
    assert len(calls) == 1
    monkeypatch.setattr(driver, 'state', lambda unit: {'InvocationID': 'e'*32, 'ExecMainPID': '123'})
    with pytest.raises(ValueError, match='identity'):
        instance.observe(op)
