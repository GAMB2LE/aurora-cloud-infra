import sys
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
import retry_coordinator as coordinator


class Adapter:
    def __init__(self):
        self.worker = None
        self.launches = 0
        self.captures = 0
        self.result = 'running'

    def observe_previous_attempt(self, identity):
        return {'identity': identity}

    def expected_version(self):
        return {'relative_path': 'test.LV1'}

    def fresh_pinned_inputs(self, proof, release):
        inputs = {key: key.encode() for key in ('continuity', 'observation', 'request', 'launcher')}
        inputs['request'] = json.dumps({'operation_id': f"retry-{'a'*32}-000001",
                                        'relative_path': 'test.LV1'}).encode()
        return inputs

    def authenticated_worker(self, unit, inputs, release):
        return self.worker

    def admit(self, intent, inputs):
        pass

    def launch(self, unit, inputs, release):
        self.launches += 1
        self.worker = {'InvocationID': 'b' * 32, 'ExecMainPID': '42'}

    def worker_result(self, unit, identity):
        return self.result

    def capture_and_register(self, intent, identity):
        self.captures += 1


@pytest.fixture
def classified(monkeypatch):
    monkeypatch.setattr(coordinator.precommit_retry, 'classify', lambda **evidence: {
        'state': 'precommit-lock-refusal', 'invocation_id': (evidence['identity'] or {}).get('invocation_id', 'c' * 32),
        'unit': (evidence['identity'] or {}).get('unit', 'aurora-direct-original.service'),
        'journal_sha256': 'd' * 64, 'expected': {'relative_path': 'test.LV1'}})


def test_running_worker_is_reconnected_not_restarted(tmp_path, classified):
    adapter = Adapter()
    args = (tmp_path, 'a' * 32, 1, 'pinned-release', adapter)
    assert coordinator.advance(*args) == 'waiting-for-worker'
    assert coordinator.advance(*args) == 'waiting-for-worker'
    assert adapter.launches == 1
    adapter.result = 'success'
    assert coordinator.advance(*args) == 'captured'
    assert adapter.captures == 1


def test_missing_recorded_worker_is_not_relaunched(tmp_path, classified):
    adapter = Adapter()
    args = (tmp_path, 'a' * 32, 1, 'pinned-release', adapter)
    assert coordinator.advance(*args) == 'waiting-for-worker'
    adapter.worker = None
    with pytest.raises(coordinator.UnresolvedAttempt, match='missing or changed'):
        coordinator.advance(*args)
    assert adapter.launches == 1


def test_crash_after_launch_reconnects_authenticated_worker(tmp_path, classified):
    adapter = Adapter()
    original_launch = adapter.launch
    def crash_after_launch(*args):
        original_launch(*args)
        raise RuntimeError('process died after launch')
    adapter.launch = crash_after_launch
    args = (tmp_path, 'a' * 32, 1, 'pinned-release', adapter)
    with pytest.raises(RuntimeError, match='process died'):
        coordinator.advance(*args)
    assert coordinator.advance(*args) == 'waiting-for-worker'
    assert adapter.launches == 1


def test_launch_intent_without_worker_requires_reconciliation(tmp_path, classified):
    adapter = Adapter()
    def crash_before_launch(*args):
        raise RuntimeError('process died before launch')
    adapter.launch = crash_before_launch
    args = (tmp_path, 'a' * 32, 1, 'pinned-release', adapter)
    with pytest.raises(RuntimeError, match='process died'):
        coordinator.advance(*args)
    with pytest.raises(coordinator.UnresolvedAttempt, match='no identifiable worker'):
        coordinator.advance(*args)
    assert adapter.launches == 0


def test_next_attempt_requires_recorded_refusal_and_delay(tmp_path, classified):
    adapter = Adapter()
    op = 'a' * 32
    with pytest.raises(coordinator.UnresolvedAttempt, match='previous attempt evidence missing'):
        coordinator.prepare(tmp_path, op, 2, 'pinned-release', adapter, now=1000)
    adapter.result = 'lock-refused-before-commit'
    assert coordinator.advance(tmp_path, op, 1, 'pinned-release', adapter, now=1000) == 'waiting-for-retry'
    # Re-observing the same failed worker must not extend the deadline.
    assert coordinator.advance(tmp_path, op, 1, 'pinned-release', adapter, now=1100) == 'waiting-for-retry'
    with pytest.raises(coordinator.UnresolvedAttempt, match='delay'):
        coordinator.prepare(tmp_path, op, 2, 'pinned-release', adapter, now=1299)
    original = adapter.fresh_pinned_inputs
    def second_inputs(proof, release):
        inputs = original(proof, release)
        inputs['request'] = json.dumps({'operation_id': f'retry-{op}-000002', 'relative_path': 'test.LV1'}).encode()
        return inputs
    adapter.fresh_pinned_inputs = second_inputs
    _, intent, _, _ = coordinator.prepare(tmp_path, op, 2, 'pinned-release', adapter, now=1300)
    assert intent['attempt'] == 2
