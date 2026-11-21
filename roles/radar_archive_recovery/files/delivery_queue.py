"""Durable independent GWS and object operation lanes for native radar delivery.

This module owns accounting only. Callers must hold the production coordinator
lock, authenticate worker identities, and independently validate receipts.
It does not launch workers or grant retention or eviction authority.
"""
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from gws_pending_replay import valid_expected

AUTHORITY = {'retention_authorized': False, 'eviction_authorized': False}
MAX_PENDING_FILES = 256
MAX_PENDING_BYTES = 16 * 1024**3


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class DeliveryQueue:
    def __init__(self, path):
        self.db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rwc', uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS files (
                identity TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL UNIQUE,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                cohort TEXT NOT NULL CHECK(cohort IN ('fresh', 'backfill', 'frozen')),
                gws_verified INTEGER NOT NULL DEFAULT 0 CHECK(gws_verified IN (0,1)),
                object_verified INTEGER NOT NULL DEFAULT 0 CHECK(object_verified IN (0,1)),
                CHECK(object_verified <= gws_verified)
            );
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY,
                lane TEXT NOT NULL CHECK(lane IN ('gws','object')),
                identity TEXT NOT NULL REFERENCES files(identity),
                intent TEXT NOT NULL,
                started REAL NOT NULL,
                finished REAL,
                receipt TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_per_lane
                ON operations(lane) WHERE finished IS NULL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT OR IGNORE INTO settings VALUES ('fresh_streak', 0);
            INSERT OR IGNORE INTO settings VALUES ('object_fresh_streak', 0);
            CREATE TABLE IF NOT EXISTS lane_waits (
                lane TEXT PRIMARY KEY CHECK(lane IN ('gws','object')),
                operation_id TEXT NOT NULL,
                blocker TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                retry_at REAL NOT NULL
            );
        ''')

    def close(self):
        self.db.close()

    def add(self, expected, cohort):
        if not valid_expected(expected) or cohort not in {'fresh', 'backfill', 'frozen'}:
            raise ValueError('invalid exact file version or cohort')
        key = packed(expected)
        with self.db:
            row = self.db.execute('SELECT identity,cohort FROM files WHERE relative_path=?',
                                  (expected['relative_path'],)).fetchone()
            if row:
                if row['identity'] != key or row['cohort'] != cohort:
                    raise ValueError('path already has a different version or cohort')
                return
            self.db.execute('INSERT INTO files(identity,relative_path,size,mtime_ns,cohort) VALUES (?,?,?,?,?)',
                            (key, expected['relative_path'], expected['size'], expected['mtime_ns'], cohort))

    def outstanding(self, lane):
        if lane not in {'gws', 'object'}:
            raise ValueError('unknown destination lane')
        row = self.db.execute('SELECT * FROM operations WHERE lane=? AND finished IS NULL', (lane,)).fetchone()
        if row is None:
            return None
        return {'id': row['id'], 'lane': lane, 'expected': json.loads(row['identity']),
                'intent': json.loads(row['intent']), 'started': row['started']}

    def _pending_versions(self, external_pending):
        if not isinstance(external_pending, list) or any(not valid_expected(item) for item in external_pending):
            raise ValueError('global GWS-only inventory is missing or invalid')
        pending = {packed(item): item for item in external_pending}
        for item in self.db.execute('''SELECT identity FROM files
                                       WHERE gws_verified=1 AND object_verified=0'''):
            expected_pending = json.loads(item['identity'])
            pending[packed(expected_pending)] = expected_pending
        return pending

    def _choose(self, rows, counter):
        if not rows:
            return None
        fresh = [r for r in rows if r['cohort'] == 'fresh']
        older = [r for r in rows if r['cohort'] != 'fresh']
        streak = self.db.execute('SELECT value FROM settings WHERE key=?', (counter,)).fetchone()[0]
        if older and (not fresh or streak >= 3):
            selected = min(older, key=lambda r: (r['mtime_ns'], r['relative_path']))
        else:
            selected = min(fresh, key=lambda r: (not r['relative_path'].endswith('.LV1'),
                                                  -r['mtime_ns'], r['relative_path']))
        return json.loads(selected['identity'])

    def next_gws(self, *, external_pending):
        if self.outstanding('gws') is not None:
            return None
        pending = self._pending_versions(external_pending)
        used = sum(item['size'] for item in pending.values())
        paths = {item['relative_path'] for item in pending.values()}
        rows = [row for row in self.db.execute('SELECT * FROM files WHERE gws_verified=0')
                if len(pending) < MAX_PENDING_FILES and row['size'] + used <= MAX_PENDING_BYTES
                and row['relative_path'] not in paths]
        return self._choose(rows, 'fresh_streak')

    def next_object(self):
        if self.outstanding('object') is not None:
            return None
        rows = self.db.execute('SELECT * FROM files WHERE gws_verified=1 AND object_verified=0').fetchall()
        return self._choose(rows, 'object_fresh_streak')

    def reserve(self, lane, expected, intent, *, external_pending=None, now=None):
        if lane not in {'gws', 'object'} or not valid_expected(expected) or not isinstance(intent, dict) or not intent:
            raise ValueError('invalid operation reservation')
        key = packed(expected)
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT * FROM files WHERE identity=?', (key,)).fetchone()
            if row is None or self.outstanding(lane) is not None:
                raise ValueError('file absent or destination lane already has an operation')
            wait = self.recovery_wait(lane, None)
            if wait and now < wait['retry_at']:
                raise ValueError('lane admission backoff has not elapsed')
            if wait:
                self.db.execute('DELETE FROM lane_waits WHERE lane=?', (lane,))
            if lane == 'gws':
                if row['gws_verified']:
                    raise ValueError('GWS version already verified')
                pending = self._pending_versions(external_pending)
                if any(item['relative_path'] == expected['relative_path'] for item in pending.values()):
                    raise ValueError('path already has a GWS-only version')
                if (len(pending) + 1 > MAX_PENDING_FILES
                        or sum(item['size'] for item in pending.values()) + row['size'] > MAX_PENDING_BYTES):
                    raise ValueError('GWS-only backlog capacity would be exceeded')
                if row['cohort'] == 'fresh':
                    self.db.execute("UPDATE settings SET value=value+1 WHERE key='fresh_streak'")
                else:
                    self.db.execute("UPDATE settings SET value=0 WHERE key='fresh_streak'")
            elif not row['gws_verified'] or row['object_verified']:
                raise ValueError('object operation requires an unpaired verified GWS version')
            elif row['cohort'] == 'fresh':
                self.db.execute("UPDATE settings SET value=value+1 WHERE key='object_fresh_streak'")
            else:
                self.db.execute("UPDATE settings SET value=0 WHERE key='object_fresh_streak'")
            operation_id = uuid4().hex
            self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,NULL,NULL)',
                            (operation_id, lane, key, packed(intent), now))
        return self.outstanding(lane)

    def accept(self, operation, receipt, *, now=None):
        lane = operation.get('lane')
        if (lane not in {'gws', 'object'} or not isinstance(receipt, dict)
                or receipt.get('operation_id') != operation.get('id')
                or receipt.get('expected') != operation.get('expected')
                or receipt.get('lane') != lane or receipt.get('verified') is not True
                or not isinstance(receipt.get('evidence'), dict) or not receipt['evidence']
                or any(receipt.get(key) is not value for key, value in AUTHORITY.items())):
            raise ValueError('receipt does not bind exact destination operation')
        evidence = receipt['evidence']
        if (evidence.get('gws_readback_verified') is not True
                or evidence.get('sha256') != operation['expected']['sha256']):
            raise ValueError('independent GWS version proof missing')
        if lane == 'object' and (evidence.get('object_readback_verified') is not True
                or evidence.get('pair_recorded') is not True or evidence.get('ledger_replicated') is not True
                or not isinstance(evidence.get('snapshot_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', evidence['snapshot_sha256'])):
            raise ValueError('object pair and replicated checkpoint proof missing')
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.outstanding(lane) != operation:
                raise ValueError('operation no longer outstanding')
            column = 'gws_verified' if lane == 'gws' else 'object_verified'
            changed = self.db.execute(f'UPDATE files SET {column}=1 WHERE identity=? AND {column}=0',
                                      (packed(operation['expected']),))
            if changed.rowcount != 1:
                raise ValueError('file stage changed')
            self.db.execute('UPDATE operations SET finished=?,receipt=? WHERE id=?',
                            (now, packed(receipt), operation['id']))
            self.db.execute('DELETE FROM lane_waits WHERE lane=? AND operation_id=?', (lane, operation['id']))

    def recovery_wait(self, lane, operation_id):
        if lane not in {'gws', 'object'}:
            raise ValueError('unknown destination lane')
        key = operation_id or ''
        row = self.db.execute('SELECT * FROM lane_waits WHERE lane=?', (lane,)).fetchone()
        if row is None:
            return None
        if row['operation_id'] != key:
            raise ValueError('persisted wait belongs to a different operation')
        return {**dict(row), 'persistent_alert': row['attempts'] >= 3}

    def defer(self, lane, operation_id, blocker, *, now=None):
        if (lane not in {'gws', 'object'} or not isinstance(blocker, str)
                or not re.fullmatch('[a-z][a-z0-9-]{0,63}', blocker)):
            raise ValueError('invalid lane deferral')
        if operation_id is not None and (not isinstance(operation_id, str) or not re.fullmatch('[0-9a-f]{32}', operation_id)):
            raise ValueError('invalid deferred operation')
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            current = self.outstanding(lane)
            if (current['id'] if current else None) != operation_id:
                raise ValueError('deferred operation differs from the lane')
            previous = self.recovery_wait(lane, operation_id)
            if previous and now < previous['retry_at']:
                return previous
            attempts = previous['attempts'] + 1 if previous and previous['blocker'] == blocker else 1
            self.db.execute('''INSERT INTO lane_waits VALUES (?,?,?,?,?) ON CONFLICT(lane) DO UPDATE SET
                            operation_id=excluded.operation_id,blocker=excluded.blocker,
                            attempts=excluded.attempts,retry_at=excluded.retry_at''',
                            (lane, operation_id or '', blocker, attempts, now + 300))
        return self.recovery_wait(lane, operation_id)

    def status(self):
        row = self.db.execute('''SELECT COUNT(*) AS files,
                 COALESCE(SUM(CASE WHEN gws_verified=1 AND object_verified=0 THEN 1 ELSE 0 END),0) AS pending_files,
                 COALESCE(SUM(CASE WHEN gws_verified=1 AND object_verified=0 THEN size ELSE 0 END),0) AS pending_bytes,
                 COALESCE(SUM(object_verified),0) AS object_verified FROM files''').fetchone()
        return {'files': row['files'], 'gws_only_files': row['pending_files'],
                'gws_only_bytes': row['pending_bytes'], 'object_verified': row['object_verified'],
                'gws_operation': self.outstanding('gws'), 'object_operation': self.outstanding('object'),
                'gws_wait': self.recovery_wait('gws', (self.outstanding('gws') or {}).get('id')),
                'object_wait': self.recovery_wait('object', (self.outstanding('object') or {}).get('id')),
                **AUTHORITY}
