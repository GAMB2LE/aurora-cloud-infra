import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
import fresh_gws_canary as canary
from per_file_queue import Queue


@pytest.fixture(autouse=True)
def empty_global_pending(monkeypatch):
    monkeypatch.setattr(canary.coordinator, 'classify_pending', lambda *args: [])


def test_canaries_keep_separate_queue_and_never_advance_object(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, 'BASE', tmp_path)
    monkeypatch.setattr(canary, 'QUEUE', tmp_path / 'fresh.sqlite')
    monkeypatch.setattr(canary.coordinator, 'STATE', tmp_path / 'frozen.sqlite')
    frozen = Queue(canary.coordinator.STATE)
    frozen.close()
    monkeypatch.setattr(canary, 'require_supervisor_held', lambda: None)
    monkeypatch.setattr(canary.coordinator, 'require_reconciled', lambda base: None)
    monkeypatch.setattr(canary.coordinator, 'check_retention_and_fence', lambda: None)
    def prepare(candidate, **kwargs):
        return {}, {'relative_path': candidate['relative_path'], 'size': candidate['size'],
                    'mtime_ns': candidate['mtime'] * 10**9, 'sha256': 'a' * 64}
    monkeypatch.setattr(canary.coordinator, 'prepare_one', prepare)
    class Driver:
        def __init__(self, prepared): pass
        def reserve(self, expected, target):
            assert target == 'gws-verified'
            return {'preparation': 'proof'}
        def apply(self, op):
            assert op['target'] == 'gws-verified'
        def observe(self, op):
            return {'operation_id': op['id'], 'expected': op['expected'], 'stage': op['target'],
                    'verified': True, 'evidence': {'proof': True},
                    'retention_authorized': False, 'eviction_authorized': False}
    monkeypatch.setattr(canary.coordinator, 'Driver', Driver)
    for suffix in ('LV1', 'LV0'):
        result = canary.start({'relative_path': 'Y2026/M11/D13/test.' + suffix,
                               'size': 1, 'mtime': 1})
    assert result['counts']['gws-verified'] == 2
    assert result['counts']['object-verified'] == 0
    frozen = Queue(canary.coordinator.STATE, readonly=True)
    assert sum(frozen.status()['counts'].values()) == 0
    frozen.close()
    with pytest.raises(ValueError, match='already has a recorded version'):
        canary.start({'relative_path': 'Y2026/M11/D13/test.LV1', 'size': 1, 'mtime': 1})


def test_canary_refuses_unresolved_frozen_operation(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, 'BASE', tmp_path)
    monkeypatch.setattr(canary, 'QUEUE', tmp_path / 'fresh.sqlite')
    monkeypatch.setattr(canary.coordinator, 'STATE', tmp_path / 'frozen.sqlite')
    monkeypatch.setattr(canary, 'require_supervisor_held', lambda: None)
    queue = Queue(canary.coordinator.STATE)
    expected = {'relative_path': 'Y2026/M11/D13/test.LV1', 'size': 1,
                'mtime_ns': 1, 'sha256': 'a' * 64}
    queue.add(expected)
    queue.begin(expected, 'gws-verified', {'preparation': 'test'})
    queue.close()
    with pytest.raises(ValueError, match='frozen cohort has an unresolved operation'):
        canary.start({'relative_path': 'Y2026/M11/D13/test.LV0', 'size': 1, 'mtime': 1})


def test_canary_does_not_exceed_gws_only_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, 'BASE', tmp_path)
    monkeypatch.setattr(canary, 'QUEUE', tmp_path / 'fresh.sqlite')
    monkeypatch.setattr(canary.coordinator, 'STATE', tmp_path / 'frozen.sqlite')
    monkeypatch.setattr(canary, 'require_supervisor_held', lambda: None)
    Queue(canary.coordinator.STATE).close()
    with pytest.raises(ValueError, match='backlog capacity'):
        canary.start({'relative_path': 'Y2026/M11/D13/test.LV0', 'size': 16 * 1024**3 + 1, 'mtime': 1})


def test_canary_capacity_includes_other_queues(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, 'BASE', tmp_path)
    monkeypatch.setattr(canary, 'QUEUE', tmp_path / 'fresh.sqlite')
    monkeypatch.setattr(canary.coordinator, 'STATE', tmp_path / 'frozen.sqlite')
    monkeypatch.setattr(canary, 'require_supervisor_held', lambda: None)
    Queue(canary.coordinator.STATE).close()
    pending = [{'status': 'object-copy-needed', 'expected': {
        'relative_path': f'Y2026/M11/D13/old{i}.LV1', 'size': 1,
        'mtime_ns': 1, 'sha256': 'a' * 64}} for i in range(256)]
    monkeypatch.setattr(canary.coordinator, 'classify_pending', lambda *args: pending)
    with pytest.raises(ValueError, match='backlog capacity'):
        canary.start({'relative_path': 'Y2026/M11/D13/new.LV1', 'size': 1, 'mtime': 1})


def test_replication_targets_only_separate_canary_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, 'BASE', tmp_path)
    monkeypatch.setattr(canary, 'QUEUE', tmp_path / 'fresh.sqlite')
    monkeypatch.setattr(canary, 'require_supervisor_held', lambda: None)
    monkeypatch.setattr(canary.coordinator, 'require_reconciled', lambda base: None)
    monkeypatch.setattr(canary.coordinator, 'check_retention_and_fence', lambda: None)
    queue = Queue(canary.QUEUE)
    expected = {'relative_path': 'Y2026/M11/D13/test.LV1', 'size': 1,
                'mtime_ns': 1, 'sha256': 'a' * 64}
    queue.add(expected)
    op = queue.begin(expected, 'gws-verified', {'preparation': 'test'})
    queue.accept(op, {'operation_id': op['id'], 'expected': expected, 'stage': 'gws-verified',
                      'verified': True, 'evidence': {'proof': True},
                      'retention_authorized': False, 'eviction_authorized': False})
    queue.close()
    class Driver:
        def reserve(self, identity, target):
            assert identity == expected and target == 'object-verified'
            return {'preparation': 'object-proof'}
        def apply(self, op):
            assert canary.pair_retry.STATE == canary.QUEUE
            assert op['target'] == 'object-verified'
        def observe(self, op): return None
    monkeypatch.setattr(canary.coordinator, 'Driver', Driver)
    result = canary.replicate()
    assert result['result'] == 'awaiting-reconciliation'
    assert result['outstanding']['target'] == 'object-verified'
