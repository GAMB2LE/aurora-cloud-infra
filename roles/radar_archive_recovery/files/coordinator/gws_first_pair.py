"""Validate and record a GWS-first pair as provisional point-in-time evidence.

The caller must authenticate live readbacks, serialize ledger writes, and
replicate and independently verify a ledger snapshot. This does not prove
immutability, archive completeness, or authority to release science data.
"""
import datetime
import hashlib
import json
import sqlite3

import provisional_object_evidence
from gws_pending_replay import match_recorded_object, valid_expected


def fresh(stamp, now):
    if not isinstance(stamp, str): return False
    try: parsed = datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError: return False
    return parsed.tzinfo is not None and 0 <= (now-parsed).total_seconds() <= 300


def import_pair(db, expected, object_receipt_sha, writer_raw, gws_evidence, object_evidence, *, now):
    if not valid_expected(expected): raise ValueError('invalid expected version')
    rows = db.execute('''SELECT receipt_sha256,relative_path,size,mtime_ns,content_sha256,object_key,
                         request_sha256,writer_sha256,recorded_at,receipt_json,request_json
                         FROM provisional_object_versions WHERE receipt_sha256=?''', (object_receipt_sha,)).fetchall()
    if len(rows) != 1: raise ValueError('object ledger record missing or ambiguous')
    receipt_sha, path, size, mtime, sha, key, request_sha, writer_sha, recorded_at, receipt, request = rows[0]
    if (path, size, mtime, sha) != (expected['relative_path'], expected['size'], expected['mtime_ns'], expected['sha256']):
        raise ValueError('recorded object version differs')
    scratch = sqlite3.connect(':memory:')
    try:
        provisional_object_evidence.import_receipt(scratch, receipt, receipt_sha, request, request_sha,
                                                    writer_raw, writer_sha, recorded_at)
    finally: scratch.close()
    if not (isinstance(gws_evidence, dict) and fresh(gws_evidence.get('checked_at'), now)
            and gws_evidence.get('expected') == expected and gws_evidence.get('object_verified') is False
            and gws_evidence.get('eviction_authorized') is False and gws_evidence.get('retention_authorized') is False):
        raise ValueError('fresh GWS evidence invalid')
    match_recorded_object(expected, {**expected, 'object_key': key}, gws_evidence.get('proof'))
    if not (isinstance(object_evidence, dict) and fresh(object_evidence.get('checked_at'), now)
            and object_evidence.get('object_key') == key and object_evidence.get('expected') == expected
            and object_evidence.get('metadata_before') == object_evidence.get('metadata_after')
            and isinstance(object_evidence.get('metadata_before'), dict)
            and object_evidence['metadata_before'].get('key') == key
            and object_evidence['metadata_before'].get('size') == size
            and object_evidence.get('readback', {}).get('read_bytes') == size
            and object_evidence.get('readback', {}).get('sha256') == sha
            and object_evidence.get('eviction_authorized') is False and object_evidence.get('retention_authorized') is False):
        raise ValueError('fresh object readback invalid')
    payload = {'schema': 'radar-gws-first-pair-v1', 'expected': expected, 'object_key': key,
               'object_receipt_sha256': receipt_sha, 'gws_evidence': gws_evidence, 'object_evidence': object_evidence,
               'immutable_backing_proven': False, 'eviction_authorized': False, 'retention_authorized': False}
    raw = (json.dumps(payload, sort_keys=True, separators=(',', ':'))+'\n').encode()
    digest = hashlib.sha256(raw).hexdigest()
    db.execute('''CREATE TABLE IF NOT EXISTS provisional_gws_first_pairs (
        receipt_sha256 TEXT PRIMARY KEY, relative_path TEXT NOT NULL, size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL, content_sha256 TEXT NOT NULL, object_key TEXT NOT NULL, receipt_json BLOB NOT NULL)''')
    values = (digest, path, size, mtime, sha, key, raw)
    with db:
        db.execute('INSERT OR IGNORE INTO provisional_gws_first_pairs VALUES(?,?,?,?,?,?,?)', values)
        stored = db.execute('SELECT * FROM provisional_gws_first_pairs WHERE receipt_sha256=?', (digest,)).fetchone()
        if tuple(stored) != values: raise ValueError('pair receipt conflict')
    return {'receipt_sha256': digest, 'state': 'provisional-pair-recorded', 'ledger_replicated': False,
            'immutable_backing_proven': False, 'eviction_authorized': False, 'retention_authorized': False}
