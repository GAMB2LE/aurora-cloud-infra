"""Recovery state machine for one staged GWS file; not a launcher.

The caller must authenticate the original journal, own the recovery locks and
provide fresh controls. This module never grants permission to remove data.
"""
import json
import os
import time
from pathlib import Path

import direct_interrupt
import direct_staged_commit
import direct_staged_recovery as recovery


def resume(adapter, observation, preflight, journal_raw, journal_path, *, clock=time.time, apply=False):
    recovery._validate_observation(observation, clock())
    events = [json.loads(line) for line in journal_raw.splitlines()]
    expected = preflight.get('expected', {})
    source = preflight.get('source', {})
    operation = preflight.get('operation', {})
    if not (preflight.get('schema') == 'radar-direct-backlog-preflight-v1'
            and [e.get('phase') for e in events] == ['intent', 'stage-complete']
            and set(expected) == {'relative_path', 'size', 'mtime_ns', 'sha256'}
            and set(operation) == {'nonce', 'source_script_sha256', 'receiver_script_sha256', 'destination'}
            and operation.get('destination') == 'gws-canonical'
            and events[0].get('operation') == operation
            and all(e.get(k) == v for e in events for k, v in expected.items())
            and all(e.get('eviction_authorized') is False and e.get('retention_authorized') is False for e in [preflight, *events])
            and events[1].get('source_stable') is True
            and expected.get('relative_path') == observation['relative_path']
            and expected.get('size') == observation['size']):
        raise ValueError('original stage evidence differs')
    stage = events[1].get('stage_receipt', {})
    if not (stage.get('schema') == 'radar-direct-gws-staged-v1'
            and stage.get('canonical_published') is False
            and stage.get('eviction_authorized') is False and stage.get('retention_authorized') is False
            and all(stage.get(k) == v for k, v in expected.items())
            and stage.get('nonce') == operation.get('nonce')):
        raise ValueError('stage receipt differs')
    recovery._validate_source(observation, source)
    path = Path(journal_path)
    commit_path = Path(str(path) + '.commit.jsonl')
    fence_path = Path(str(path) + '.fence-reconciliation.json')
    readback_path = Path(str(path) + '.readback.json')
    with direct_interrupt.protect(), adapter.exclusive_recovery_locks():
        adapter.set_deadline(time.monotonic() + 7200)
        if any(os.path.lexists(p) for p in (commit_path, fence_path, readback_path)):
            raise ValueError('publication attempt requires reconciliation')
        adapter.require_original_worker_terminal()
        adapter.require_controls_capacity_and_hold()
        adapter.require_source_observation(observation)
        fresh = adapter.hash_settled_source(observation['relative_path'], timeout=1800)
        recovery._validate_source(observation, fresh)
        if fresh != source:
            raise ValueError('source version changed')
        recovery._validate_observation(observation, clock())
        adapter.require_source_identity(source)
        adapter.require_local_absence(expected['relative_path'])
        adapter.require_gws_absence_and_capacity(expected['relative_path'], expected['size'])
        adapter.require_controls_capacity_and_hold()
        if not apply:
            verified = adapter.verify_staged(stage, timeout=1800)
            if not (verified.get('stage') == stage and verified.get('sha256') == expected['sha256']
                    and verified.get('size') == expected['size'] and verified.get('eviction_authorized') is False
                    and verified.get('retention_authorized') is False):
                raise ValueError('staged readback differs')
            return {'mode': 'read-only', 'gws_verified': False, 'stage_verified': True,
                    'object_verified': False, 'eviction_authorized': False, 'retention_authorized': False}
        publication = direct_staged_commit.publish(adapter, stage, commit_path, timeout=3600)
        adapter.require_source_identity(source)
        fence = adapter.reconcile_successful_publication(publication['verified_stage'], fence_path, timeout=1800)
        if fence.get('outcome', {}).get('state') != 'present':
            raise ValueError('publication reconciliation differs')
        proof = adapter.independent_gws_readback(expected, timeout=1800)
        if not (all(proof.get(k) == v for k, v in expected.items()) and proof.get('stable') is True):
            raise ValueError('independent GWS proof differs')
        recovery._save_exclusive(readback_path, {'schema': 'radar-direct-backlog-gws-readback-v1',
            'expected': expected, 'proof': proof, 'stream_result': events[1], 'publication': publication,
            'fence_reconciliation': fence, 'object_verified': False, 'immutable_backing_proven': False,
            'eviction_authorized': False, 'retention_authorized': False})
    return {'gws_verified': True, 'object_verified': False, 'eviction_authorized': False,
            'retention_authorized': False}
