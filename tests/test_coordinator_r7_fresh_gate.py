import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
import per_file_coordinator_r7 as coordinator
from per_file_queue import Queue
from per_file_queue import writer_lock


def test_fresh_gate_reads_durable_operation_and_rejects_symlink(tmp_path, monkeypatch):
    path = tmp_path / 'fresh.sqlite'
    monkeypatch.setattr(coordinator, 'FRESH_STATE', path)
    assert coordinator.fresh_outstanding() is False
    old_umask = os.umask(0o077)
    try:
        queue = Queue(path)
    finally:
        os.umask(old_umask)
    item = {'relative_path': 'Y2026/M11/D13/example.LV1', 'size': 1, 'mtime_ns': 2, 'sha256': 'a'*64}
    queue.add(item)
    assert coordinator.fresh_outstanding() is False
    operation = queue.begin(item, 'gws-verified', {'unit': 'worker'})
    assert coordinator.fresh_outstanding() is True
    queue.accept(operation, {'operation_id': operation['id'], 'expected': item, 'stage': 'gws-verified',
                             'verified': True, 'evidence': {'receipt': 'ok'},
                             'retention_authorized': False, 'eviction_authorized': False})
    assert coordinator.fresh_outstanding() is False
    queue.close()
    renamed = tmp_path / 'actual.sqlite'; path.rename(renamed); path.symlink_to(renamed)
    with pytest.raises(ValueError, match='not protected'):
        coordinator.fresh_outstanding()


def test_running_coordinator_releases_lock_while_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, 'BASE', tmp_path)
    monkeypatch.setattr(coordinator, 'STATE', tmp_path / 'frozen.sqlite')
    monkeypatch.setattr(coordinator, 'FRESH_STATE', tmp_path / 'fresh.sqlite')
    monkeypatch.setattr(coordinator, 'publish_status', lambda value: None)
    monkeypatch.setattr(sys, 'argv', ['coordinator', 'run', '--steps', '2', '--watch-existing'])
    calls = []
    def step(queue, candidates, *, reconcile_only, long_gws):
        calls.append(reconcile_only)
        return ['awaiting-reconciliation', 'advanced', 'queue-exhausted'][len(calls)-1]
    monkeypatch.setattr(coordinator, 'step', step)
    original = coordinator.coordinator_step_loop.run
    def run(tick, **kwargs):
        def sleep(_):
            with writer_lock(tmp_path / 'per-file-coordinator.lock'):
                assert True
        return original(tick, **kwargs, sleep=sleep)
    monkeypatch.setattr(coordinator.coordinator_step_loop, 'run', run)
    coordinator.main()
    assert calls == [False, True, False]


def test_recent_gws_object_copy_waits_without_blocking_mature_work(monkeypatch):
    now = 2_000_000_000
    recent = {'relative_path': 'recent', 'mtime_ns': (now-3600)*10**9}
    mature = {'relative_path': 'mature', 'mtime_ns': (now-21600)*10**9}
    paired = {'relative_path': 'already-paired', 'mtime_ns': now*10**9}
    rows = [{'expected': recent, 'status': 'object-copy-needed'},
            {'expected': mature, 'status': 'object-copy-needed'},
            {'expected': paired, 'status': 'pair-reconciliation-needed'}]
    monkeypatch.setattr(coordinator, 'classify_pending', lambda *args: rows)
    monkeypatch.setattr(coordinator.time, 'time', lambda: now)
    class Stub:
        def __init__(self): self.items = []
        def add(self, item): self.items.append(item)
    queue = Stub()
    assert coordinator.import_pending(queue) == 1
    assert queue.items == [mature, paired]


def test_finished_or_missing_backfill_does_not_stop_historical_coordinator(monkeypatch):
    from types import SimpleNamespace
    state = {'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'exited',
             'MainPID': '0', 'Result': 'success', 'ExecMainStatus': '0'}
    monkeypatch.setattr(coordinator.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        stdout='\n'.join(f'{key}={value}' for key, value in state.items())))
    assert coordinator.native_backfill_waiting() is False
    state['ExecMainStatus'] = '1'
    with pytest.raises(ValueError, match='without a successful result'):
        coordinator.native_backfill_waiting()
    state.update(LoadState='not-found', ActiveState='inactive')
    assert coordinator.native_backfill_waiting() is False
