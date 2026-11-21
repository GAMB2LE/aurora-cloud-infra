"""Classify an authenticated failed GWS attempt before authorizing a retry.

This module neither launches workers nor changes archive state.  Its caller
must obtain each observation freshly while holding the coordinator locks.
"""
import hashlib
import json
import re


class ReconciliationRequired(ValueError):
    """The prior attempt may have published or its identity is uncertain."""


def classify(*, expected, preflight_raw, journal_raw, recorded_identity,
             actual_worker, worker_pids, worker_log, artifacts,
             gws_absent, stage_proof, source_proof, holds_ok, capacity_ok):
    """Return a retry proof only for a fully evidenced pre-commit refusal."""
    def require(condition, reason):
        if not condition:
            raise ReconciliationRequired(reason)

    require(isinstance(recorded_identity, dict) and isinstance(actual_worker, dict), 'worker identity missing')
    unit = recorded_identity.get('unit')
    invocation = recorded_identity.get('invocation_id')
    pid = recorded_identity.get('main_pid')
    require(isinstance(unit, str) and re.fullmatch(r'aurora-direct-[a-z0-9-]+\.service', unit), 'unit invalid')
    require(isinstance(invocation, str) and re.fullmatch(r'[0-9a-f]{32}', invocation), 'invocation invalid')
    require(type(pid) is int and pid > 0, 'worker pid invalid')
    require(actual_worker.get('unit') == unit and actual_worker.get('InvocationID') == invocation
            and actual_worker.get('ExecMainPID') == str(pid)
            and actual_worker.get('ActiveState') == 'failed' and actual_worker.get('SubState') == 'failed'
            and actual_worker.get('Result') == 'exit-code' and actual_worker.get('ExecMainStatus') == '1',
            'worker differs or is not terminal')
    require(worker_pids == [], 'worker descendants remain or are unknown')
    require(isinstance(worker_log, str) and worker_log.rstrip().splitlines()[-1:] ==
            ['installed_fenced_delivery.DeliveryBusy: GWS delivery deferred: active batch has not finished'],
            'failure is not the exact delivery lock refusal')
    names = {'commit', 'fence', 'fence_reconciliation', 'readback'}
    require(isinstance(artifacts, dict) and set(artifacts) == names
            and all(artifacts[name] is False for name in names), 'publication artifacts are present or unknown')
    require(gws_absent is True and holds_ok is True and capacity_ok is True, 'destination or admission unresolved')
    require(isinstance(expected, dict) and set(expected) == {'relative_path', 'size', 'mtime_ns', 'sha256'}
            and type(expected['size']) is int and expected['size'] > 0
            and type(expected['mtime_ns']) is int and expected['mtime_ns'] >= 0
            and isinstance(expected['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', expected['sha256']),
            'expected version invalid')
    require(isinstance(preflight_raw, bytes) and isinstance(journal_raw, bytes), 'journal bytes missing')
    try:
        preflight = json.loads(preflight_raw)
        events = [json.loads(line) for line in journal_raw.splitlines()]
    except (ValueError, TypeError):
        raise ReconciliationRequired('journal unparseable') from None
    require(preflight.get('schema') == 'radar-direct-backlog-preflight-v1'
            and preflight.get('expected') == expected
            and [e.get('phase') for e in events] == ['intent', 'stage-complete']
            and events[0].get('operation') == preflight.get('operation'), 'original journal differs')
    require(all(all(e.get(k) == v for k, v in expected.items()) for e in events)
            and all(e.get('retention_authorized') is False and e.get('eviction_authorized') is False for e in [preflight, *events])
            and events[1].get('source_stable') is True, 'original version or authority differs')
    stage = events[1].get('stage_receipt', {})
    require(stage.get('schema') == 'radar-direct-gws-staged-v1'
            and stage.get('canonical_published') is False
            and all(stage.get(k) == v for k, v in expected.items())
            and stage.get('nonce') == preflight.get('operation', {}).get('nonce')
            and stage.get('retention_authorized') is False and stage.get('eviction_authorized') is False,
            'stage receipt differs')
    require(isinstance(stage_proof, dict) and stage_proof.get('schema') == 'radar-direct-gws-stage-verified-v1'
            and stage_proof.get('stage') == stage and stage_proof.get('partial_identity') == stage.get('partial_identity')
            and isinstance(stage.get('partial_identity'), list) and len(stage['partial_identity']) == 9
            and stage_proof.get('sha256') == expected['sha256'] and stage_proof.get('size') == expected['size']
            and stage_proof.get('retention_authorized') is False and stage_proof.get('eviction_authorized') is False,
            'fresh staged readback differs')
    require(isinstance(source_proof, dict) and all(source_proof.get(k) == v for k, v in expected.items())
            and source_proof.get('stable') is True, 'fresh source version differs')
    return {'state': 'precommit-lock-refusal', 'unit': unit, 'invocation_id': invocation,
            'expected': expected, 'preflight_sha256': hashlib.sha256(preflight_raw).hexdigest(),
            'journal_sha256': hashlib.sha256(journal_raw).hexdigest(),
            'retention_authorized': False, 'eviction_authorized': False}
