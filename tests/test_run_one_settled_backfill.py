from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
import run_one_settled_backfill as runner


def test_missing_queue_after_start_is_not_reported_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'FRESH', tmp_path/'missing.sqlite')
    operation = {'id': 'a'*32, 'expected': {'name': 'x'}}
    monkeypatch.setattr(runner, 'start', lambda *a: {'state': 'started', 'operation': operation})
    with pytest.raises(ValueError, match='without a verified receipt'):
        runner.run('/manifest', 'digest', clock=lambda: 0, sleep=lambda _: None)


def test_existing_uncertain_operation_is_never_relaunched(tmp_path, monkeypatch):
    from per_file_queue import Queue
    path = tmp_path/'fresh.sqlite'; monkeypatch.setattr(runner, 'FRESH', path)
    old = runner.os.umask(0o077)
    try: queue = Queue(path)
    finally: runner.os.umask(old)
    item = {'relative_path': 'Y2026/M11/D08/example.LV1', 'size': 10, 'mtime_ns': 1, 'sha256': 'a'*64}
    queue.add(item); queue.begin(item, 'gws-verified', {'unit': 'existing'}); queue.close()
    monkeypatch.setattr(runner, 'start', lambda *a: pytest.fail('must not launch another operation'))
    monkeypatch.setattr(runner, 'reconcile', lambda: {'state': 'awaiting-reconciliation'})
    clock = [0]
    def sleep(_): clock[0] += 10
    with pytest.raises(TimeoutError, match='unresolved'):
        runner.run('/manifest', 'digest', clock=lambda: clock[0], sleep=sleep, timeout=20)
