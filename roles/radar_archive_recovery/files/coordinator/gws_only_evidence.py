"""Validate one completed field-to-GWS operation without claiming dual copies.

This validator does not authenticate its inputs, replicate a ledger, establish
source inventory completeness, or authorize deletion of local data.
"""
import hashlib
import json
import re
import datetime

from gws_receive import PATH, SHA, MAX_SIZE


def finalized_source(expected, preflight):
    handoff = preflight.get('handoff')
    if not isinstance(handoff, dict): return False
    try:
        stamp = datetime.datetime.fromisoformat(preflight['at'].replace('Z', '+00:00'))
        age = stamp.timestamp() - handoff['post_stage_observed_at']
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    identity = handoff.get('stage_identity')
    return (stamp.tzinfo is not None and expected['relative_path'].endswith('.LV1')
            and handoff.get('schema') == 'radar-protected-stage-handoff-v1'
            and all(handoff.get(key) == expected[field] for key, field in
                    (('source', 'relative_path'), ('size', 'size'), ('source_mtime_ns', 'mtime_ns'), ('sha256', 'sha256')))
            and isinstance(identity, list) and len(identity) == 6 and all(type(x) is int and x >= 0 for x in identity)
            and identity[2] == expected['size'] and identity[5] == 1
            and all(isinstance(handoff.get(key), str) and re.fullmatch('[0-9a-f]{32}', handoff[key]) for key in
                    ('stage_operation', 'post_stage_operation', 'finalization_invocation_id'))
            and type(handoff.get('post_stage_observed_at')) is int and 0 <= age <= 300
            and type(handoff.get('checked_at')) is int and handoff['post_stage_observed_at'] <= handoff['checked_at'] <= stamp.timestamp()
            and all(handoff.get(key) is False for key in ('fast_archive_authorized', 'retention_authorized', 'eviction_authorized')))


def validate(expected, preflight, journal_raw, commit_raw, fence, readback):
    if not (isinstance(expected, dict) and set(expected) == {'relative_path', 'size', 'mtime_ns', 'sha256'}
            and isinstance(expected['relative_path'], str) and PATH.fullmatch(expected['relative_path'])
            and type(expected['size']) is int and 0 < expected['size'] <= MAX_SIZE
            and type(expected['mtime_ns']) is int and 0 <= expected['mtime_ns'] < 2**63
            and isinstance(expected['sha256'], str) and SHA.fullmatch(expected['sha256'])):
        raise ValueError('invalid expected version')
    events = [json.loads(line) for line in journal_raw.splitlines()]
    commits = [json.loads(line) for line in commit_raw.splitlines()]
    operation = preflight.get('operation', {})
    source = preflight.get('source', {})
    if not isinstance(source, dict): source = {}
    source_identity = source.get('identity', [])
    legacy_source = (preflight.get('schema') == 'radar-direct-backlog-preflight-v1'
                     and source.get('relative_path') == expected['relative_path'] and source.get('sha256') == expected['sha256']
                     and isinstance(source_identity, list) and len(source_identity) == 9
                     and all(type(x) is int and x >= 0 for x in source_identity)
                     and source_identity[2] == expected['size'] and source_identity[3] == expected['mtime_ns']
                     and source_identity[8] == 1)
    staged_source = preflight.get('schema') == 'radar-finalized-stage-preflight-v1' and finalized_source(expected, preflight)
    if not (len(events) == 2 and [x.get('phase') for x in events] == ['intent', 'stage-complete']
            and len(commits) == 2 and [x.get('phase') for x in commits] == ['commit-intent', 'commit-complete']
            and (legacy_source or staged_source)
            and preflight.get('expected') == expected
            and isinstance(operation, dict)
            and set(operation) == {'nonce', 'source_script_sha256', 'receiver_script_sha256', 'destination'}
            and operation.get('destination') == 'gws-canonical'
            and isinstance(operation.get('nonce'), str) and re.fullmatch('[0-9a-f]{32}', operation['nonce'])
            and all(isinstance(operation.get(k), str) and SHA.fullmatch(operation[k])
                    for k in ('source_script_sha256', 'receiver_script_sha256'))
            and events[0].get('operation') == operation
            and all(e.get(k) == v for e in events for k, v in expected.items())
            and all(e.get('eviction_authorized') is False and e.get('retention_authorized') is False
                    for e in [preflight, *events, *commits])):
        raise ValueError('transport evidence differs')
    result = events[1]
    stage = result.get('stage_receipt', {})
    verified = commits[0].get('verified_stage', {})
    committed = commits[1].get('receipt', {})
    proof_sha = hashlib.sha256(json.dumps(verified, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if not (result.get('source_stable') is True
            and stage.get('schema') == 'radar-direct-gws-staged-v1'
            and stage.get('nonce') == events[0]['operation'].get('nonce')
            and all(stage.get(k) == v for k, v in expected.items())
            and isinstance(stage.get('partial_identity'), list) and len(stage['partial_identity']) == 9
            and all(type(x) is int and x >= 0 for x in stage['partial_identity'])
            and stage['partial_identity'][2] == expected['size']
            and stage['partial_identity'][3] == expected['mtime_ns'] and stage['partial_identity'][8] == 1
            and stage.get('canonical_published') is False
            and verified.get('schema') == 'radar-direct-gws-stage-verified-v1'
            and verified.get('stage') == stage and verified.get('partial_identity') == stage.get('partial_identity')
            and verified.get('sha256') == expected['sha256'] and verified.get('size') == expected['size']
            and committed.get('schema') == 'radar-direct-gws-committed-v1'
            and all(committed.get(k) == v for k, v in expected.items())
            and committed.get('atomic_no_replace') is True
            and all(x.get('eviction_authorized') is False and x.get('retention_authorized') is False
                    for x in (stage, verified, committed))):
        raise ValueError('GWS publication evidence differs')
    proof = readback.get('proof', {})
    if not (readback.get('schema') == ('radar-finalized-stage-gws-readback-v1' if staged_source else 'radar-direct-backlog-gws-readback-v1')
            and readback.get('expected') == expected and readback.get('stream_result') == result
            and readback.get('publication', {}).get('verified_stage') == verified
            and readback.get('publication', {}).get('receipt') == committed
            and readback.get('fence_reconciliation') == fence
            and fence.get('schema') == 'aurora-gws-fence-reconciliation-v1'
            and fence.get('expected') == expected and fence.get('stage') == stage
            and fence.get('proof_sha256') == proof_sha
            and fence.get('remote_ready', {}).get('proof_sha256') == proof_sha
            and fence.get('remote_ready', {}).get('nonce') == stage.get('nonce')
            and fence.get('terminal_evidence', {}).get('state') == 'terminal'
            and fence.get('outcome', {}).get('state') == 'present'
            and fence.get('outcome', {}).get('stable') is True
            and all(fence.get('outcome', {}).get(k) == v for k, v in expected.items())
            and fence.get('eviction_authorized') is False and fence.get('retention_authorized') is False
            and all(proof.get(k) == v for k, v in expected.items()) and proof.get('stable') is True
            and readback.get('object_verified') is False and readback.get('immutable_backing_proven') is False
            and readback.get('eviction_authorized') is False and readback.get('retention_authorized') is False):
        raise ValueError('independent GWS readback differs')
    return {'state': 'gws-present-verified', 'expected': expected, 'object_verified': False,
            'point_in_time_only': True, 'immutable_backing_proven': False,
            'eviction_authorized': False, 'retention_authorized': False}
