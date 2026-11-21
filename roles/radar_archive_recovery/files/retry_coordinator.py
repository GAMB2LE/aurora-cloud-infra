"""Coordinate one pre-commit retry under the existing archive writer locks.

The adapter supplies authenticated observations and pinned systemd operations;
unknown outcomes stay pending and are never interpreted as permission to retry.
"""
from pathlib import Path
import json
import time

import precommit_retry
import retry_attempts


class UnresolvedAttempt(RuntimeError):
    """A launched worker or its outcome cannot be authenticated."""


def prepare(root, operation_id, number, release, adapter, *, now=None):
    """Adopt a durable attempt, or verify and reserve a fresh one."""
    if type(number) is not int or not 1 <= number <= 999999:
        raise ValueError('invalid attempt number')
    path = Path(root) / f'{operation_id}-{number:06d}'
    if path.exists():
        intent, inputs, identity = retry_attempts.load(path)
        if intent['operation_id'] != operation_id or intent['attempt'] != number or intent['release'] != release:
            raise UnresolvedAttempt('recorded attempt differs from operation')
        return path, intent, inputs, identity
    previous_identity = None
    if number > 1:
        previous_path = Path(root) / f'{operation_id}-{number-1:06d}'
        try:
            previous, _, previous_identity = retry_attempts.load(previous_path)
            deferred = retry_attempts.load_deferral(previous_path)
        except (FileNotFoundError, ValueError) as error:
            raise UnresolvedAttempt('previous attempt evidence missing') from error
        if (previous['operation_id'] != operation_id or previous['attempt'] != number-1
                or previous['release'] != release or previous_identity is None
                or deferred is None or deferred['identity'] != previous_identity):
            raise UnresolvedAttempt('previous attempt is not a recorded lock refusal')
        if (time.time() if now is None else now) < deferred['retry_at']:
            raise UnresolvedAttempt('retry delay has not elapsed')
    evidence = adapter.observe_previous_attempt(previous_identity)
    proof = precommit_retry.classify(**evidence)
    if proof['expected'] != adapter.expected_version():
        raise UnresolvedAttempt('previous failure refers to a different file version')
    if previous_identity is not None and (proof['invocation_id'] != previous_identity['invocation_id']
                                          or proof['unit'] != previous_identity['unit']):
        raise UnresolvedAttempt('previous failure refers to a different worker')
    inputs = adapter.fresh_pinned_inputs(proof, release)
    try:
        request = json.loads(inputs['request'])
    except (KeyError, TypeError, ValueError):
        raise UnresolvedAttempt('retry request cannot be decoded') from None
    if (request.get('operation_id') != f'retry-{operation_id}-{number:06d}'
            or request.get('relative_path') != proof['expected'].get('relative_path')):
        raise UnresolvedAttempt('retry request differs from the reserved worker or source')
    intent = retry_attempts.reserve(root, operation_id, number, proof, inputs, release)
    return path, intent, inputs, None


def advance(root, operation_id, number, release, adapter, *, now=None):
    """Launch once, reconnect after a crash, and capture only verified success."""
    path, intent, inputs, identity = prepare(root, operation_id, number, release, adapter, now=now)
    unit = intent['unit']
    launch_recorded = retry_attempts.has_launch_intent(path)
    if identity is not None and not launch_recorded:
        raise UnresolvedAttempt('worker identity exists without launch intent')
    observed = adapter.authenticated_worker(unit, inputs, release)
    if identity is None:
        if observed is None:
            if launch_recorded:
                raise UnresolvedAttempt('recorded launch has no identifiable worker')
            adapter.admit(intent, inputs)
            retry_attempts.record_launch_intent(path, unit)
            adapter.launch(unit, inputs, release)
            observed = adapter.authenticated_worker(unit, inputs, release)
            if observed is None:
                raise UnresolvedAttempt('launched worker cannot be identified')
        elif not launch_recorded:
            raise UnresolvedAttempt('unrecorded worker already exists')
        identity = {'unit': unit, 'invocation_id': observed['InvocationID'],
                    'main_pid': int(observed['ExecMainPID'])}
        retry_attempts.record_identity(path, identity)
    elif observed is None or (observed.get('InvocationID') != identity['invocation_id'] or
                              observed.get('ExecMainPID') != str(identity['main_pid'])):
        raise UnresolvedAttempt('recorded worker missing or changed')
    result = adapter.worker_result(unit, identity)
    if result == 'running':
        return 'waiting-for-worker'
    if result == 'lock-refused-before-commit':
        retry_attempts.defer(path, identity, time.time() if now is None else now)
        return 'waiting-for-retry'
    if result != 'success':
        raise UnresolvedAttempt('worker outcome needs reconciliation')
    adapter.capture_and_register(intent, identity)
    return 'captured'
