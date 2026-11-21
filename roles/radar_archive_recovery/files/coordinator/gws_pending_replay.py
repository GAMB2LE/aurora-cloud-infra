"""Select exact GWS-only versions for later object-copy admission.

No transfer, ledger pairing, object verification, or deletion is performed.
The caller must independently verify the current GWS copy and all live controls.
"""
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from gws_only_evidence import validate
from gws_receive import MAX_SIZE, PATH, SHA
from provisional_object_evidence import BASE as OBJECT_BASE


def valid_expected(value):
    return (isinstance(value, dict) and set(value) == {'relative_path', 'size', 'mtime_ns', 'sha256'}
            and isinstance(value['relative_path'], str) and PATH.fullmatch(value['relative_path']) is not None
            and type(value['size']) is int and 0 < value['size'] <= MAX_SIZE
            and type(value['mtime_ns']) is int and 0 <= value['mtime_ns'] < 2**63
            and isinstance(value['sha256'], str) and SHA.fullmatch(value['sha256']) is not None)


def verified_pending(database):
    db = sqlite3.connect('file:' + str(Path(database).resolve()) + '?mode=ro', uri=True)
    try:
        db.execute('BEGIN')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('pending database integrity failed')
        rows = db.execute('SELECT relative_path,size,mtime_ns,sha256,evidence_sha256,evidence_json FROM pending ORDER BY relative_path,size,mtime_ns,sha256').fetchall()
        out = []
        for path, size, mtime, sha, evidence_sha, evidence_raw in rows:
            if hashlib.sha256(evidence_raw).hexdigest() != evidence_sha:
                raise ValueError('pending evidence digest differs')
            value = json.loads(evidence_raw)
            expected = {'relative_path': path, 'size': size, 'mtime_ns': mtime, 'sha256': sha}
            if value.get('expected') != expected:
                raise ValueError('pending version identity differs')
            parts = db.execute('SELECT name,content_sha256,content FROM artifacts WHERE relative_path=? AND size=? AND mtime_ns=? AND sha256=?',
                               (path, size, mtime, sha)).fetchall()
            names = {'preflight.json', 'journal.jsonl', 'commit.jsonl', 'fence.json', 'readback.json'}
            if len(parts) != len(names) or {part[0] for part in parts} != names:
                raise ValueError('pending evidence bundle incomplete')
            raw = {}
            for name, digest, content in parts:
                if hashlib.sha256(content).hexdigest() != digest or value.get('evidence_sha256', {}).get(name) != digest:
                    raise ValueError('pending artifact digest differs')
                raw[name] = content
            proof = validate(expected, json.loads(raw['preflight.json']), raw['journal.jsonl'], raw['commit.jsonl'],
                             json.loads(raw['fence.json']), json.loads(raw['readback.json']))
            if any(value.get(key) != result for key, result in proof.items()):
                raise ValueError('pending verification differs')
            out.append(expected)
        return out
    finally:
        db.close()


def require_fresh_source(expected, request):
    """Bind a new read-only admission to the exact earlier GWS version."""
    source = request.get('expected_source', {}) if isinstance(request, dict) else {}
    identity = source.get('identity', []) if isinstance(source, dict) else []
    if not (valid_expected(expected) and isinstance(request, dict) and request.get('relative_path') == expected.get('relative_path')
            and isinstance(identity, list) and len(identity) == 10
            and all(type(item) is int and item >= 0 for item in identity)
            and identity[2] == expected.get('size') and identity[3] == expected.get('mtime_ns')
            and source.get('sha256') == expected.get('sha256')):
        raise ValueError('fresh source differs from pending GWS version')
    return {'accepted': True, 'relative_path': expected['relative_path'],
            'object_verified': False, 'eviction_authorized': False, 'retention_authorized': False}


def classify_pending(pending_database, operational_database):
    """Classify exact versions without treating a ledger row as release proof."""
    pending = verified_pending(pending_database)
    db = sqlite3.connect('file:' + str(Path(operational_database).resolve()) + '?mode=ro', uri=True)
    try:
        db.execute('BEGIN')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'provisional_object_versions' not in tables:
            raise ValueError('object ledger table missing')
        result = []
        for item in pending:
            identity = (item['relative_path'], item['size'], item['mtime_ns'], item['sha256'])
            objects = db.execute('''SELECT receipt_sha256,object_key FROM provisional_object_versions
                                    WHERE relative_path=? AND size=? AND mtime_ns=? AND content_sha256=?''', identity).fetchall()
            if len(objects) > 1: raise ValueError('multiple object versions require reconciliation')
            status = 'object-copy-needed' if not objects else 'pair-reconciliation-needed'
            if objects and 'provisional_gws_first_pairs' in tables:
                pairs = db.execute('''SELECT receipt_sha256,receipt_json FROM provisional_gws_first_pairs
                                      WHERE relative_path=? AND size=? AND mtime_ns=? AND content_sha256=? AND object_key=?''',
                                   (*identity, objects[0][1])).fetchall()
                if len(pairs) > 1: raise ValueError('multiple pair records require reconciliation')
                if pairs:
                    digest, raw = pairs[0]
                    value = json.loads(raw)
                    if not (hashlib.sha256(raw).hexdigest() == digest and value.get('schema') == 'radar-gws-first-pair-v1'
                            and value.get('expected') == item and value.get('object_key') == objects[0][1]
                            and value.get('object_receipt_sha256') == objects[0][0]
                            and value.get('immutable_backing_proven') is False
                            and value.get('eviction_authorized') is False and value.get('retention_authorized') is False):
                        raise ValueError('pair receipt differs')
                    status = 'provisional-pair-recorded'
            result.append({'expected': item, 'status': status, 'eviction_authorized': False, 'retention_authorized': False})
        return result
    finally:
        db.close()


def match_recorded_object(expected, ledger_row, fresh_gws):
    """Compare a previously imported object version with a fresh GWS readback.

    Caller must authenticate the ledger row and readback, verify current object
    contents, and publish a durable ledger before treating this as a pair.
    """
    if not (valid_expected(expected) and isinstance(ledger_row, dict)
            and all(ledger_row.get(k) == expected.get(k) for k in ('relative_path', 'size', 'mtime_ns', 'sha256'))
            and isinstance(ledger_row.get('object_key'), str)
            and re.fullmatch(re.escape(OBJECT_BASE) + r'[0-9a-f]{32}/' + expected['sha256'] + r'\.radar', ledger_row['object_key'])
            and isinstance(fresh_gws, dict) and fresh_gws.get('stable') is True
            and all(fresh_gws.get(k) == expected.get(k) for k in ('relative_path', 'size', 'mtime_ns', 'sha256'))):
        raise ValueError('object or fresh GWS version differs')
    return {'state': 'matching-point-in-time-evidence', 'expected': expected,
            'object_key': ledger_row['object_key'], 'object_freshness_verified': False,
            'immutable_backing_proven': False, 'ledger_replicated': False,
            'eviction_authorized': False, 'retention_authorized': False}
