"""Durable local queue of verified GWS-only versions, not an eviction ledger."""
import hashlib
import json
import sqlite3
from pathlib import Path

from gws_receive import MAX_SIZE, PATH, SHA
from gws_only_evidence import validate


def register_captured(preparation, database):
    """Revalidate captured evidence before recording a pending object copy."""
    directory = Path(preparation) / 'captured-gws-evidence'
    verification = json.loads((directory / 'verification.json').read_bytes())
    names = {'preflight.json', 'journal.jsonl', 'commit.jsonl', 'fence.json', 'readback.json'}
    hashes = verification.get('evidence_sha256')
    if not isinstance(hashes, dict) or set(hashes) != names:
        raise ValueError('captured evidence inventory differs')
    raw = {name: (directory / name).read_bytes() for name in names}
    if any(hashlib.sha256(raw[name]).hexdigest() != hashes[name] for name in names):
        raise ValueError('captured evidence changed')
    proof = validate(verification['expected'], json.loads(raw['preflight.json']), raw['journal.jsonl'],
                     raw['commit.jsonl'], json.loads(raw['fence.json']), json.loads(raw['readback.json']))
    if any(verification.get(key) != value for key, value in proof.items()):
        raise ValueError('captured verification differs')
    return record(database, verification, evidence_raw=raw)


def record(database, verification, *, evidence_raw=None):
    if not (isinstance(verification, dict) and verification.get('state') == 'gws-present-verified'
            and verification.get('object_verified') is False and verification.get('point_in_time_only') is True
            and verification.get('immutable_backing_proven') is False
            and verification.get('eviction_authorized') is False and verification.get('retention_authorized') is False):
        raise ValueError('not a GWS-only verification')
    row = verification.get('expected', {})
    if not (isinstance(row, dict) and set(row) == {'relative_path', 'size', 'mtime_ns', 'sha256'}
            and isinstance(row['relative_path'], str) and PATH.fullmatch(row['relative_path'])
            and type(row['size']) is int and 0 < row['size'] <= MAX_SIZE
            and type(row['mtime_ns']) is int and 0 <= row['mtime_ns'] < 2**63
            and isinstance(row['sha256'], str) and SHA.fullmatch(row['sha256'])):
        raise ValueError('invalid GWS-only version')
    raw = (json.dumps(verification, sort_keys=True, separators=(',', ':')) + '\n').encode()
    digest = hashlib.sha256(raw).hexdigest()
    names = {'preflight.json', 'journal.jsonl', 'commit.jsonl', 'fence.json', 'readback.json'}
    if evidence_raw is not None:
        hashes = verification.get('evidence_sha256')
        if not isinstance(evidence_raw, dict) or set(evidence_raw) != names or not isinstance(hashes, dict) or set(hashes) != names:
            raise ValueError('invalid evidence bundle')
        if any(not isinstance(evidence_raw[name], bytes) or len(evidence_raw[name]) > 1024 * 1024
               or hashlib.sha256(evidence_raw[name]).hexdigest() != hashes[name] for name in names):
            raise ValueError('evidence bundle digest differs')
    db = sqlite3.connect(str(Path(database)), timeout=10, isolation_level=None)
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('''CREATE TABLE IF NOT EXISTS pending (
            relative_path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL, evidence_sha256 TEXT NOT NULL, evidence_json BLOB NOT NULL,
            PRIMARY KEY(relative_path, size, mtime_ns, sha256))''')
        db.execute('''CREATE TABLE IF NOT EXISTS artifacts (
            relative_path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL, name TEXT NOT NULL, content_sha256 TEXT NOT NULL, content BLOB NOT NULL,
            PRIMARY KEY(relative_path, size, mtime_ns, sha256, name))''')
        db.execute('BEGIN IMMEDIATE')
        try:
            values = (row['relative_path'], row['size'], row['mtime_ns'], row['sha256'], digest, raw)
            old = db.execute('SELECT * FROM pending WHERE relative_path=? AND size=? AND mtime_ns=? AND sha256=?', values[:4]).fetchone()
            if old is None: db.execute('INSERT INTO pending VALUES(?,?,?,?,?,?)', values)
            elif tuple(old) != values: raise ValueError('conflicting GWS evidence for version')
            if evidence_raw is not None:
                for name in sorted(names):
                    artifact = (*values[:4], name, hashlib.sha256(evidence_raw[name]).hexdigest(), evidence_raw[name])
                    prior = db.execute('SELECT * FROM artifacts WHERE relative_path=? AND size=? AND mtime_ns=? AND sha256=? AND name=?', artifact[:5]).fetchone()
                    if prior is None: db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)', artifact)
                    elif tuple(prior) != artifact: raise ValueError('conflicting artifact for version')
            db.commit()
        except BaseException:
            db.rollback(); raise
    finally:
        db.close()
    return {'state': 'object-copy-pending', 'relative_path': row['relative_path'], 'evidence_sha256': digest,
            'eviction_authorized': False, 'retention_authorized': False}
