"""Close a proven pre-transfer GWS failure without advancing any archive stage."""
import hashlib
import re
import time
from per_file_queue import AUTHORITY, packed
SCHEMA = 'radar-unstarted-gws-attempt-v1'
RESOLUTION = 'radar-handoff-retired-unstarted-gws-v1'


def validate(operation, receipt):
    if (operation.get('target') != 'gws-verified' or receipt.get('schema') != SCHEMA
            or receipt.get('operation_id') != operation.get('id')
            or receipt.get('expected') != operation.get('expected')
            or receipt.get('unit') != operation.get('intent', {}).get('unit')
            or receipt.get('intent_sha256') != hashlib.sha256(packed(operation['intent']).encode()).hexdigest()
            or receipt.get('reason') != 'expired-handoff-before-transfer'
            or receipt.get('worker_exit_code') != 1
            or receipt.get('verified') is not False or receipt.get('transfer_started') is not False
            or any(receipt.get(k) is not True for k in
                   ('worker_terminal', 'transfer_journal_absent', 'gws_canonical_absent', 'gws_partials_absent'))
            or any(receipt.get(k) is not value for k, value in AUTHORITY.items())
            or any(not isinstance(receipt.get(k), str) or not re.fullmatch('[0-9a-f]{64}', receipt[k])
                   for k in ('worker_log_sha256', 'request_sha256', 'handoff_sha256'))
            or not isinstance(receipt.get('worker_invocation_id'), str)
            or not re.fullmatch('[0-9a-f]{32}', receipt['worker_invocation_id'])):
        raise ValueError('unstarted GWS retirement does not bind the exact failed attempt')
    return receipt


def retire(queue, operation, receipt):
    validate(operation, receipt)
    with queue.db:
        queue.db.execute('BEGIN IMMEDIATE')
        if queue.outstanding() != operation:
            raise ValueError('GWS attempt is no longer outstanding')
        row = queue.db.execute('SELECT stage FROM files WHERE identity=?', (packed(operation['expected']),)).fetchone()
        if row is None or row['stage'] != 0:
            raise ValueError('pre-transfer retirement cannot change an advanced archive version')
        queue.db.execute('UPDATE operations SET finished=?,receipt=? WHERE id=?',
                         (time.time(), packed(receipt), operation['id']))


def validate_resolution(queue, stage, after, proof):
    import json
    from pathlib import Path
    from per_file_coordinator import BASE, candidate_identity
    if (proof.get('schema') != RESOLUTION or proof.get('stage') != stage or proof.get('after') != after):
        raise ValueError('retired admission resolution identity differs')
    row = queue.db.execute('SELECT * FROM operations WHERE id=?', (proof.get('operation_id'),)).fetchone()
    if row is None or row['finished'] is None or row['target'] != 1:
        raise ValueError('retired admission has no terminal GWS queue operation')
    expected, intent, receipt = (json.loads(row[k]) for k in ('identity', 'intent', 'receipt'))
    validate({'id': row['id'], 'expected': expected, 'intent': intent, 'target': 'gws-verified'}, receipt)
    if receipt != proof.get('retirement_receipt'):
        raise ValueError('retired admission receipt differs from the durable queue')
    out = Path(intent['preparation']['out'])
    if out.parent != BASE or not re.fullmatch(r'direct-finalized-preparation-20\d{6}T\d{6}Z', out.name):
        raise ValueError('retired admission preparation path differs')
    request_raw = (out/'request.json').read_bytes()
    request = json.loads(request_raw)
    if (request.get('finalized_stage') != {'operation': stage, 'post_stage_operation': after, 'expected': expected}
            or candidate_identity(out) != expected
            or hashlib.sha256(request_raw).hexdigest() != receipt['request_sha256']):
        raise ValueError('retired admission source or request differs')
