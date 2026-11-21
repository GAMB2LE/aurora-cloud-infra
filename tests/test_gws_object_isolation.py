import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
import gws_object_isolation as guard


def expected(name):
    return {'relative_path': 'Y2026/M11/D13/' + name, 'size': 10, 'mtime_ns': 123, 'sha256': 'a' * 64}


def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + '\n')
    path.chmod(0o600)


@pytest.fixture
def locks(tmp_path):
    result = {}
    for name in ('per-file-coordinator.lock', 'bounded-backlog-coordinator.lock'):
        result[name] = os.open(tmp_path / name, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        yield result
    finally:
        for fd in result.values(): os.close(fd)


def setup_worker(tmp_path, monkeypatch):
    prep = tmp_path / 'direct-object-gzip-preparation-20261114T010203Z'
    prep.mkdir(mode=0o700)
    source = expected('old.LV1')
    request_id = '20261027-gzip-next-20261114t010203z-0'
    unit = 'aurora-object-direct-' + request_id + '.service'
    admitted = {'accepted': True, 'invocation_id': 'b' * 32}
    marker = {'schema': 'radar-gws-pending-object-intent-v1', 'expected': source,
              'preparation': str(prep), 'unit': unit, 'admission': admitted,
              'object_key_not_yet_known': True, 'retention_authorized': False, 'eviction_authorized': False}
    admission = {'expected': source, 'preparation': str(prep), 'admission': admitted,
                 'transfer_started': False, 'object_verified': False,
                 'retention_authorized': False, 'eviction_authorized': False}
    request = {'expected_source': {'identity': [1, 2, 10, 123], 'sha256': 'a' * 64},
               'operation_id': request_id, 'relative_path': source['relative_path']}
    identity = {'unit': unit, 'invocation_id': 'c' * 32, 'main_pid': 42}
    write(tmp_path / 'gws-pending-object-unresolved.json', marker)
    write(prep / 'gws-pending-admission.json', admission)
    write(prep / 'request.json', request)
    write(prep / 'service-identity.json', identity)
    monkeypatch.setattr(guard, 'check_retention_and_fence', lambda: None)
    monkeypatch.setattr(guard, 'resolve', lambda path: ({'id': 'gws-object-vm03'}, ['pinned']))
    monkeypatch.setattr(guard, 'verify_pins', lambda *args: None)
    monkeypatch.setattr(guard, 'authenticate_worker', lambda *args: None)
    monkeypatch.setattr(guard, 'state', lambda unit: {'LoadState': 'loaded', 'InvocationID': 'c' * 32,
                         'ExecMainPID': '42', 'ActiveState': 'active', 'SubState': 'running'})
    return {'id': 'd' * 32, 'target': 'object-verified', 'expected': source,
            'intent': {'preparation': str(prep)}}


def test_authenticated_gws_source_object_isolated_from_different_candidate(tmp_path, monkeypatch, locks):
    op = setup_worker(tmp_path, monkeypatch)
    result = guard.check(expected('new.LV1'), op, lock_fds=locks, base=tmp_path)
    assert result['object_isolated'] is True and result['object_operation'] == op['id']
    assert result['retention_authorized'] is result['eviction_authorized'] is False
    with pytest.raises(ValueError, match='not safely isolated'):
        guard.check(op['expected'], op, lock_fds=locks, base=tmp_path)


def test_unknown_worker_or_changed_evidence_blocks_admission(tmp_path, monkeypatch, locks):
    op = setup_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(guard, 'state', lambda unit: {'LoadState': 'not-found'})
    with pytest.raises(ValueError, match='identity or state'):
        guard.check(expected('new.LV1'), op, lock_fds=locks, base=tmp_path)
    with pytest.raises(ValueError, match='not safely isolated'):
        guard.check(expected('new.LV1'), None, lock_fds=locks, base=tmp_path)


def test_unresolved_gws_guard_is_never_ignored(tmp_path, monkeypatch, locks):
    monkeypatch.setattr(guard, 'check_retention_and_fence', lambda: None)
    (tmp_path / 'bounded-backlog-unresolved.json').write_text('{}')
    with pytest.raises(ValueError, match='unresolved GWS'):
        guard.check(expected('new.LV1'), None, lock_fds=locks, base=tmp_path)


def test_missing_or_replaced_lock_never_authorizes_admission(tmp_path, monkeypatch, locks):
    monkeypatch.setattr(guard, 'check_retention_and_fence', lambda: None)
    with pytest.raises(ValueError, match='both archive writer'):
        guard.check(expected('new.LV1'), None, lock_fds={}, base=tmp_path)
    path = tmp_path / 'bounded-backlog-coordinator.lock'
    path.rename(tmp_path / 'old.lock')
    path.write_text('')
    path.chmod(0o600)
    with pytest.raises(ValueError, match='lock identity'):
        guard.check(expected('new.LV1'), None, lock_fds=locks, base=tmp_path)
