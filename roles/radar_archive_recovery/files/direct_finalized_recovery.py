"""Guarded finalized-stage to GWS state machine; no retention authority.

The adapter must authenticate the remote root-owned handoff, pin sender and
receiver releases, and implement the existing destination locks and fences.
This module is not a production launcher.
"""
import datetime
import hashlib
import os
from pathlib import Path
import secrets
import time

import direct_interrupt
import direct_stage_pipeline as transport
import direct_staged_commit as commit
from direct_staged_recovery_refresh import _save_exclusive
from finalized_expected import valid_expected


def transfer_one(adapter, expected, journal_path, *, safety_check, budget=7200, monotonic=time.monotonic):
    if not valid_expected(expected) or type(budget) is not int or not 0 < budget <= 7200 or not callable(safety_check):
        raise ValueError('invalid expected version or transfer budget')
    journal = Path(journal_path)
    preflight = Path(str(journal) + '.preflight.json')
    readback = Path(str(journal) + '.readback.json')
    commit_journal = Path(str(journal) + '.commit.jsonl')
    fence_receipt = Path(str(journal) + '.fence-reconciliation.json')
    continuity = Path(str(journal) + '.continuity-refresh.json')
    with direct_interrupt.protect(), adapter.exclusive_recovery_locks():
        deadline = monotonic() + budget
        adapter.set_deadline(deadline)
        def remaining():
            value = deadline - monotonic()
            if value <= 0: raise TimeoutError('finalized-stage transfer deadline exceeded')
            return value
        if any(os.path.lexists(p) for p in (preflight, journal, readback, commit_journal, fence_receipt, continuity)):
            raise ValueError('existing attempt requires reconciliation')
        adapter.require_controls_capacity_and_hold()
        handoff = adapter.authenticated_handoff(expected)
        adapter.require_local_absence(expected['relative_path'])
        adapter.require_gws_absence_and_capacity(expected['relative_path'], expected['size'])
        adapter.require_controls_capacity_and_hold()
        if adapter.authenticated_handoff(expected) != handoff:
            raise ValueError('source handoff changed during admission')
        nonce = secrets.token_hex(16)
        sender, receiver, source_raw, receiver_raw = adapter.finalized_stage_commands(expected, handoff, nonce)
        if not isinstance(source_raw, bytes) or not source_raw or not isinstance(receiver_raw, bytes) or not receiver_raw:
            raise ValueError('transport scripts are not pinned')
        operation = {'nonce': nonce, 'source_script_sha256': hashlib.sha256(source_raw).hexdigest(),
                     'receiver_script_sha256': hashlib.sha256(receiver_raw).hexdigest(), 'destination': 'gws-canonical'}
        _save_exclusive(preflight, {'schema': 'radar-finalized-stage-preflight-v1',
                        'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'expected': expected,
                        'handoff': handoff, 'operation': operation,
                        'eviction_authorized': False, 'retention_authorized': False})
        result = transport.transfer(sender, receiver, expected, journal, operation=operation, timeout=remaining(),
                                    safety_check=safety_check)
        if adapter.authenticated_handoff(expected) != handoff:
            raise ValueError('source handoff changed before publication')
        adapter.refresh_continuity(continuity, timeout=remaining())
        adapter.require_controls_capacity_and_hold()
        if adapter.authenticated_handoff(expected) != handoff:
            raise ValueError('source handoff changed after continuity refresh')
        publication = commit.publish(adapter, result['stage_receipt'], commit_journal, timeout=remaining())
        reconciliation = adapter.reconcile_successful_publication(publication['verified_stage'], fence_receipt,
                                                                    timeout=remaining())
        if reconciliation.get('outcome', {}).get('state') != 'present':
            raise ValueError('publication reconciliation differs')
        proof = adapter.independent_gws_readback(expected, timeout=remaining())
        if not (isinstance(proof, dict) and all(proof.get(k) == expected[k] for k in expected)
                and proof.get('stable') is True):
            raise ValueError('independent GWS readback differs')
        _save_exclusive(readback, {'schema': 'radar-finalized-stage-gws-readback-v1', 'expected': expected,
                        'proof': proof, 'stream_result': result, 'publication': publication,
                        'fence_reconciliation': reconciliation, 'object_verified': False,
                        'immutable_backing_proven': False,
                        'retention_authorized': False, 'eviction_authorized': False})
        return {'expected': expected, 'gws_verified': True, 'object_verified': False,
                'retention_authorized': False, 'eviction_authorized': False}
