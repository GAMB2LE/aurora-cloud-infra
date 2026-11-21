"""One bounded step of a two-lane radar delivery supervisor.

The adapter must authenticate existing workers and receipts, hold the existing
coordinator lock around this call, and provide destination-specific guard
contexts. This module does not weaken the deployed publication fences.
"""
import time
import re

DEFERRED_CODES = {
    'gws': {'gws-capacity-unavailable', 'gws-lock-busy', 'source-not-ready'},
    'object': {'object-store-unavailable', 'object-capacity-unavailable', 'pair-readback-unavailable'},
}


class LaneDeferred(RuntimeError):
    """A specifically identified, independently guarded lane is unavailable."""
    def __init__(self, code):
        if (not isinstance(code, str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}', code)
                or code not in set().union(*DEFERRED_CODES.values())):
            raise ValueError('lane deferral requires a stable blocker code')
        super().__init__(code)


def _defer(queue, lane, operation_id, error, now):
    if str(error) not in DEFERRED_CODES[lane]:
        raise ValueError('deferral code is not valid for this destination lane')
    return {'state': 'deferred', 'recovery': queue.defer(lane, operation_id, str(error), now=now)}


def _observe(queue, adapter, lane, operation, now):
    """Never relaunch an operation with a durable outstanding intent."""
    try:
        receipt = adapter.observe(lane, operation)
    except LaneDeferred as error:
        return _defer(queue, lane, operation['id'], error, now)
    if receipt is None:
        return {'state': 'awaiting-observation'}
    queue.accept(operation, receipt, now=now)
    return {'state': 'verified'}


def step(queue, adapter, *, now=None):
    """Observe or start at most one operation per lane under caller's lock.

    `gws_guard` must hold all relevant writer locks and return an authenticated
    global GWS-only inventory while proving that any outstanding object worker
    cannot mutate the candidate GWS path. `object_guard` must protect object
    admission and its destination. Unclassified failures stop the whole step.
    """
    now = time.time() if now is None else now
    result = {}
    object_op = queue.outstanding('object')
    object_wait = queue.recovery_wait('object', object_op['id'] if object_op else None)
    if object_op is not None:
        result['object'] = ({'state': 'deferred', 'recovery': object_wait}
                            if object_wait and now < object_wait['retry_at']
                            else _observe(queue, adapter, 'object', object_op, now))
    gws_op = queue.outstanding('gws')
    gws_wait = queue.recovery_wait('gws', gws_op['id'] if gws_op else None)
    if gws_op is not None:
        result['gws'] = ({'state': 'deferred', 'recovery': gws_wait}
                         if gws_wait and now < gws_wait['retry_at']
                         else _observe(queue, adapter, 'gws', gws_op, now))
    elif gws_wait and now < gws_wait['retry_at']:
        result['gws'] = {'state': 'deferred', 'recovery': gws_wait}
    else:
        try:
            with adapter.gws_guard(queue.outstanding('object')) as pending:
                candidate = queue.next_gws(external_pending=pending)
                if candidate is None:
                    result['gws'] = {'state': 'idle-or-capacity-limited'}
                else:
                    intent = adapter.prepare('gws', candidate)
                    operation = queue.reserve('gws', candidate, intent, external_pending=pending, now=now)
                    adapter.launch('gws', operation)
                    result['gws'] = {'state': 'started', 'operation_id': operation['id']}
        except LaneDeferred as error:
            current = queue.outstanding('gws')
            result['gws'] = _defer(queue, 'gws', current['id'] if current else None, error, now)
    if object_op is None:
        if object_wait and now < object_wait['retry_at']:
            result['object'] = {'state': 'deferred', 'recovery': object_wait}
            return result
        try:
            with adapter.object_guard(queue.outstanding('gws')):
                candidate = queue.next_object()
                if candidate is None:
                    result['object'] = {'state': 'idle'}
                else:
                    intent = adapter.prepare('object', candidate)
                    operation = queue.reserve('object', candidate, intent, now=now)
                    adapter.launch('object', operation)
                    result['object'] = {'state': 'started', 'operation_id': operation['id']}
        except LaneDeferred as error:
            current = queue.outstanding('object')
            result['object'] = _defer(queue, 'object', current['id'] if current else None, error, now)
    return result
