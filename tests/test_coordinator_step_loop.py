from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
from coordinator_step_loop import run


def test_waits_without_lock_and_reconciles_existing_before_new_work():
    results = iter(['awaiting-reconciliation', 'lock-busy', 'awaiting-reconciliation', 'advanced',
                    'waiting-for-fresh-gws', 'advanced'])
    calls, sleeps = [], []
    held = [False]
    def tick(*, reconcile_only):
        held[0] = True
        try:
            calls.append(reconcile_only)
            return next(results)
        finally:
            held[0] = False
    def sleep(delay):
        assert not held[0]
        sleeps.append(delay)
    assert run(tick, steps=2, watch_existing=True, clock=lambda: 100, sleep=sleep) == 'step-limit'
    assert calls == [False, True, True, True, False, False]
    assert sleeps == [5, 5, 5, 5]


def test_uncertain_work_times_out_without_relaunching():
    now = [0]
    calls = []
    def tick(*, reconcile_only):
        calls.append(reconcile_only)
        return 'awaiting-reconciliation'
    def sleep(seconds): now[0] += 10000
    with pytest.raises(RuntimeError, match='still requires reconciliation'):
        run(tick, steps=1, watch_existing=True, clock=lambda: now[0], sleep=sleep)
    assert calls == [False, True, True, True]


def test_one_shot_reconcile_reports_unresolved():
    assert run(lambda **kw: 'awaiting-reconciliation', steps=1, watch_existing=False,
               defer_as_exit=False) == 'awaiting-reconciliation'


def test_maturing_backlog_does_not_signal_completion():
    results = iter(['waiting-for-mature-pending', 'advanced'])
    waits = []
    assert run(lambda **kw: next(results), steps=1, watch_existing=True, sleep=waits.append) == 'step-limit'
    assert waits == [5]
