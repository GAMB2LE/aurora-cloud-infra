import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('precommit_retry', Path(__file__).parents[1] /
    'roles/radar_archive_recovery/files/precommit_retry.py')
retry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retry)


@pytest.fixture
def evidence():
    expected = {'relative_path': 'Y2026/M11/D03/radar.LV1', 'size': 12, 'mtime_ns': 123,
                'sha256': 'a' * 64}
    authority = {'retention_authorized': False, 'eviction_authorized': False}
    preflight = {'schema': 'radar-direct-backlog-preflight-v1', 'expected': expected,
                 'operation': {'nonce': 'b' * 32}, **authority}
    stage = {'schema': 'radar-direct-gws-staged-v1', **expected, 'nonce': 'b' * 32, 'partial_identity': [1]*9,
             'canonical_published': False, **authority}
    journal = [{'phase': 'intent', 'operation': preflight['operation'], **expected, **authority},
               {'phase': 'stage-complete', **expected, **authority,
                'source_stable': True, 'stage_receipt': stage}]
    identity = {'unit': 'aurora-direct-gws-resume-test.service', 'invocation_id': 'c' * 32, 'main_pid': 1234}
    worker = {'unit': identity['unit'], 'InvocationID': identity['invocation_id'], 'ExecMainPID': '1234',
              'ActiveState': 'failed', 'SubState': 'failed', 'Result': 'exit-code', 'ExecMainStatus': '1'}
    return dict(expected=expected, preflight_raw=json.dumps(preflight).encode(),
                journal_raw=b'\n'.join(json.dumps(e).encode() for e in journal),
                recorded_identity=identity, actual_worker=worker, worker_pids=[],
                worker_log='installed_fenced_delivery.DeliveryBusy: GWS delivery deferred: active batch has not finished',
                artifacts={'commit': False, 'fence': False, 'fence_reconciliation': False, 'readback': False},
                gws_absent=True, holds_ok=True, capacity_ok=True,
                stage_proof={'schema': 'radar-direct-gws-stage-verified-v1', 'stage': stage,
                             'partial_identity': stage['partial_identity'], 'size': 12, 'sha256': 'a' * 64, **authority},
                source_proof={**expected, 'stable': True})


def test_proven_precommit_refusal_is_retryable(evidence):
    result = retry.classify(**evidence)
    assert result['state'] == 'precommit-lock-refusal'
    assert result['eviction_authorized'] is result['retention_authorized'] is False


@pytest.mark.parametrize('field,value', [
    ('worker_pids', [1234]), ('worker_pids', None), ('worker_log', 'connection reset'),
    ('gws_absent', None), ('holds_ok', False), ('capacity_ok', False),
    ('artifacts', {'commit': True, 'fence': False, 'fence_reconciliation': False, 'readback': False}),
    ('artifacts', {'commit': None, 'fence': False, 'fence_reconciliation': False, 'readback': False}),
    ('source_proof', {}), ('stage_proof', {}),
])
def test_ambiguous_or_changed_evidence_requires_reconciliation(evidence, field, value):
    evidence[field] = value
    with pytest.raises(retry.ReconciliationRequired):
        retry.classify(**evidence)


def test_changed_worker_invocation_cannot_be_adopted(evidence):
    evidence['actual_worker']['InvocationID'] = 'd' * 32
    with pytest.raises(retry.ReconciliationRequired, match='worker differs'):
        retry.classify(**evidence)
