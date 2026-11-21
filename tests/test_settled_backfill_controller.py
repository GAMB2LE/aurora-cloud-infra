import contextlib
import time
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
import settled_backfill_controller as controller
from per_file_queue import Queue


@pytest.fixture(autouse=True)
def isolate_primary_dispatch(monkeypatch):
    monkeypatch.setattr(controller, 'primary_dispatch_delegated', lambda rows: set())
    monkeypatch.setattr(controller, 'active_repair_owned', lambda rows: set())


@pytest.mark.parametrize('delegated', [False, 'existing', 'active'])
def test_settled_backfill_reserves_once_and_does_not_overlap(tmp_path, monkeypatch, delegated):
    now = int(time.time())
    row = {'relative_path': 'Y2026/M11/D08/example.LV1', 'size': 10, 'mtime': now-22000}
    expected = {'relative_path': row['relative_path'], 'size': 10, 'mtime_ns': row['mtime']*10**9, 'sha256': 'a'*64}
    frozen = tmp_path/'frozen.sqlite'; Queue(frozen).close()
    monkeypatch.setattr(controller, 'FRESH', tmp_path/'fresh.sqlite')
    monkeypatch.setattr(controller, 'STATE', frozen)
    monkeypatch.setattr(controller, 'require_r7', lambda: None)
    monkeypatch.setattr(controller, 'archive_locks', contextlib.nullcontext)
    monkeypatch.setattr(controller, 'load_candidates', lambda *args: [row])
    monkeypatch.setattr(controller, 'require_reconciled', lambda *args: None)
    monkeypatch.setattr(controller, 'check_retention_and_fence', lambda: None)
    monkeypatch.setattr(controller.gws_only_runner, 'known_paths', lambda: set())
    monkeypatch.setattr(controller, 'classify_pending', lambda *args: [])
    monkeypatch.setattr(controller, 'prepare_one', lambda *a, **k: ({'out': '/test'}, expected))
    calls = []
    class Driver:
        def __init__(self, prep): pass
        def reserve(self, item, target): return {'preparation': {'out': '/test'}}
        def apply(self, operation): calls.append(operation)
    monkeypatch.setattr(controller, 'Driver', Driver)
    if delegated:
        monkeypatch.setattr(controller, 'active_repair_owned' if delegated == 'active' else 'primary_dispatch_delegated', lambda rows: {row['relative_path']})
    result = controller.start('/unused', 'digest')
    if delegated:
        assert result['state'] == 'no-admissible-candidate' and not calls
        assert result['retention_authorized'] is False and result['eviction_authorized'] is False
        with contextlib.closing(Queue(controller.FRESH, readonly=True)) as fresh:
            assert fresh.outstanding() is None
        return
    assert result['state'] == 'started' and len(calls) == 1
    with pytest.raises(ValueError, match='reconciliation'):
        controller.start('/unused', 'digest')
    assert len(calls) == 1


def test_backfill_can_start_during_authenticated_lane_object_copy(tmp_path, monkeypatch):
    now = int(time.time())
    row = {'relative_path': 'Y2026/M11/D08/example.LV1', 'size': 10, 'mtime': now-22000}
    expected = {'relative_path': row['relative_path'], 'size': 10, 'mtime_ns': row['mtime']*10**9, 'sha256': 'a'*64}
    old = {**expected, 'relative_path': 'Y2026/M11/D05/other.LV1'}
    frozen_path = tmp_path/'frozen.sqlite'; frozen = Queue(frozen_path); frozen.add(old)
    first = frozen.begin(old, 'gws-verified', {'unit': 'prior'})
    frozen.accept(first, {'operation_id': first['id'], 'expected': old, 'stage': 'gws-verified', 'verified': True,
                          'evidence': {'proof': True}, 'retention_authorized': False, 'eviction_authorized': False})
    frozen.begin(old, 'object-verified', {'preparation': '/test/object'}); frozen.close()
    monkeypatch.setattr(controller, 'FRESH', tmp_path/'fresh.sqlite'); monkeypatch.setattr(controller, 'STATE', frozen_path)
    monkeypatch.setattr(controller, 'require_r7', lambda: None)
    monkeypatch.setattr(controller, 'archive_locks', lambda: contextlib.nullcontext({'locks': 1}))
    monkeypatch.setattr(controller, 'load_candidates', lambda *a: [row])
    monkeypatch.setattr(controller, 'classify_pending', lambda *a: [])
    monkeypatch.setattr(controller, 'resolve_object_release', lambda path: ({'id': 'gws-object-vm01-lanes'}, []))
    monkeypatch.setattr(controller, 'candidate_version', lambda item: expected)
    monkeypatch.setattr(controller, 'check_isolation', lambda *a, **k: {'object_isolated': True})
    monkeypatch.setattr(controller.gws_only_runner, 'known_paths', lambda **k: set())
    monkeypatch.setattr(controller, 'prepare_one', lambda *a, **k: ({'out': '/test'}, expected))
    monkeypatch.setenv('RADAR_GWS_WORKER_RELEASE', 'gzip-v5-vm01-lanes')
    calls = []
    class Driver:
        def __init__(self, prep): pass
        def reserve(self, item, target): return {'preparation': {'out': '/test'}}
        def apply(self, operation): calls.append(operation)
    monkeypatch.setattr(controller, 'Driver', Driver)
    assert controller.start('/unused', 'digest')['state'] == 'started'
    assert len(calls) == 1
