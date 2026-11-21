"""Run durable coordinator steps without holding writer locks while waiting."""
import time


def run(tick, *, steps, watch_existing, defer_as_exit=True, clock=time.monotonic, sleep=time.sleep):
    if type(steps) is not int or steps < 1:
        raise ValueError('invalid step budget')
    completed = 0
    pending_deadline = None
    while completed < steps:
        result = tick(reconcile_only=pending_deadline is not None)
        if result == 'lock-busy':
            if not watch_existing:
                raise SystemExit(75)
            if pending_deadline is not None and clock() >= pending_deadline:
                raise RuntimeError('recorded worker still requires reconciliation after wait window')
            sleep(5)
            continue
        if result == 'awaiting-reconciliation' and watch_existing:
            if pending_deadline is None:
                pending_deadline = clock() + 6 * 3600 + 1200
            if clock() >= pending_deadline:
                raise RuntimeError('recorded worker still requires reconciliation after wait window')
            sleep(5)
            continue
        pending_deadline = None
        if result in {'waiting-for-fresh-gws', 'waiting-for-mature-pending', 'waiting-for-native-backfill'} and watch_existing:
            sleep(5)
            continue
        if result == 'waiting-for-recovery' or (result == 'awaiting-reconciliation' and not watch_existing):
            if defer_as_exit:
                raise SystemExit(75)
            return result
        if result != 'advanced':
            return result
        completed += 1
    return 'step-limit'
