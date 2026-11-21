"""Durable state for the serial radar archive coordinator.

An intent survives a lost controller. Only a verified observation may resolve
it; the queue never treats an exception, missing process or timeout as success.
"""
from contextlib import contextmanager
import fcntl
import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from gws_pending_replay import valid_expected

STAGES = ('discovered', 'gws-verified', 'object-verified', 'pair-recorded', 'checkpoint-replicated')
AUTHORITY = {'retention_authorized': False, 'eviction_authorized': False}
FIELDS = ('relative_path', 'size', 'mtime_ns', 'sha256')


def packed(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False)


@contextmanager
def writer_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class Queue:
    def __init__(self, path, *, readonly=False):
        self.path = Path(path).resolve()
        self.db = sqlite3.connect(self.path.as_uri() + ('?mode=ro' if readonly else '?mode=rwc'), uri=True)
        self.db.row_factory = sqlite3.Row
        if not readonly:
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS files (
                    identity TEXT PRIMARY KEY,
                    stage INTEGER NOT NULL CHECK(stage BETWEEN 0 AND 4)
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL REFERENCES files,
                    target INTEGER NOT NULL CHECK(target BETWEEN 1 AND 4),
                    started REAL NOT NULL,
                    intent TEXT NOT NULL,
                    finished REAL,
                    receipt TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_outstanding
                ON operations ((1)) WHERE finished IS NULL;
                CREATE TABLE IF NOT EXISTS recovery_wait (
                    operation_id TEXT PRIMARY KEY REFERENCES operations(id),
                    blocker TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    retry_at REAL NOT NULL
                );
            """)
        self.db.execute('PRAGMA foreign_keys=ON')

    def close(self):
        self.db.close()

    def add(self, expected):
        if not valid_expected(expected):
            raise ValueError('invalid exact file version')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO files VALUES (?,0)', (packed(expected),))

    def next(self):
        row = self.db.execute('SELECT identity,stage FROM files WHERE stage < 4 ORDER BY stage DESC,rowid LIMIT 1').fetchone()
        return (json.loads(row['identity']), STAGES[row['stage']]) if row else None

    def outstanding(self):
        row = self.db.execute('SELECT * FROM operations WHERE finished IS NULL').fetchone()
        if row:
            return {'id':row['id'], 'expected':json.loads(row['identity']),
                    'target':STAGES[row['target']], 'started':row['started'],
                    'intent':json.loads(row['intent'])}

    def finished_receipt(self, operation_id):
        row = self.db.execute('SELECT receipt FROM operations WHERE id=? AND finished IS NOT NULL',
                              (operation_id,)).fetchone()
        return json.loads(row['receipt']) if row else None

    def retired_object_operation(self, preparation):
        rows = self.db.execute("""
            SELECT id FROM operations WHERE finished IS NOT NULL
                AND json_extract(receipt, '$.schema') = 'radar-absent-object-attempt-v1'
                AND json_extract(receipt, '$.preparation') = ?
        """, (str(preparation),)).fetchall()
        if len(rows) != 1:
            raise ValueError('guard has no unique retired object operation')
        return rows[0]['id']

    def defer_recovery(self, operation_id, blocker, *, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("""
                INSERT INTO recovery_wait VALUES (?, ?, 1, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    attempts = CASE WHEN blocker = excluded.blocker THEN attempts + 1 ELSE 1 END,
                    blocker = excluded.blocker, retry_at = excluded.retry_at
            """, (operation_id, blocker, now + 300))
        return self.recovery_wait(operation_id)

    def recovery_wait(self, operation_id):
        row = self.db.execute('SELECT * FROM recovery_wait WHERE operation_id = ?',
                              (operation_id,)).fetchone()
        if row:
            return {**dict(row), 'persistent_alert':row['attempts'] >= 3}

    def begin(self, expected, target, intent):
        """Persist the reserved worker/destination before allowing any launch."""
        index = STAGES.index(target)
        if not index or not valid_expected(expected) or not isinstance(intent, dict) or not intent:
            raise ValueError('invalid operation intent')
        key = packed(expected)
        op = uuid4().hex
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT stage FROM files WHERE identity=?', (key,)).fetchone()
            if row is None or row['stage'] != index - 1 or self.outstanding() is not None:
                raise ValueError('operation would skip a stage or overlap unresolved work')
            self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,NULL,NULL)',
                            (op,key,index,time.time(),packed(intent)))
        return self.outstanding()

    def accept(self, operation, receipt):
        """Commit a caller-validated receipt and stage atomically."""
        if (not isinstance(receipt, dict)
                or receipt.get('operation_id') != operation['id']
                or receipt.get('expected') != operation['expected']
                or receipt.get('stage') != operation['target']
                or receipt.get('verified') is not True
                or any(receipt.get(k) is not v for k,v in AUTHORITY.items())
                or not isinstance(receipt.get('evidence'), dict) or not receipt['evidence']):
            raise ValueError('receipt does not bind exact operation, stage and authority')
        index = STAGES.index(operation['target'])
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.outstanding() != operation:
                raise ValueError('operation no longer outstanding')
            result = self.db.execute('UPDATE files SET stage=? WHERE identity=? AND stage=?',
                                     (index,packed(operation['expected']),index-1))
            if result.rowcount != 1:
                raise ValueError('file stage changed')
            self.db.execute('UPDATE operations SET finished=?,receipt=? WHERE id=?',
                            (time.time(),packed(receipt),operation['id']))

    def retire_absent_object_attempt(self, operation, evidence):
        """Close an independently reconciled absent upload without advancing the file."""
        if (operation.get('target') != 'object-verified'
                or evidence.get('schema') != 'radar-absent-object-attempt-v1'
                or evidence.get('operation_id') != operation.get('id')
                or evidence.get('expected') != operation.get('expected')
                or evidence.get('object_absent') is not True
                or evidence.get('multipart_absent') is not True
                or evidence.get('failed_key_reuse_authorized') is not False
                or any(evidence.get(key) is not value for key,value in AUTHORITY.items())):
            raise ValueError('invalid absent upload reconciliation')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.outstanding() != operation:
                raise ValueError('upload is no longer outstanding')
            row = self.db.execute('SELECT stage FROM files WHERE identity=?',
                                  (packed(operation['expected']),)).fetchone()
            if row is None or row['stage'] != STAGES.index('gws-verified'):
                raise ValueError('upload file stage differs')
            self.db.execute('UPDATE operations SET finished=?,receipt=? WHERE id=?',
                            (time.time(),packed(evidence),operation['id']))

    def status(self):
        self.db.execute('BEGIN')
        try:
            counts = dict.fromkeys(STAGES, 0)
            for row in self.db.execute('SELECT stage,count(*) AS n FROM files GROUP BY stage'):
                counts[STAGES[row['stage']]] = row['n']
            return {'schema':'radar-per-file-status-v1', 'counts':counts,
                    'outstanding':self.outstanding(), **AUTHORITY}
        finally:
            self.db.rollback()


def advance(queue, driver, *, reconcile_only=False):
    """Observe an outstanding operation before selecting more work.

    driver.reserve is read-only with respect to science data. driver.apply
    is invoked only after durable intent; driver.observe independently resolves
    the exact recorded attempt. None means the outcome is still uncertain.
    """
    op = queue.outstanding()
    if op is not None:
        receipt = driver.observe(op)
        if receipt is None:
            return 'awaiting-reconciliation'
        queue.accept(op, receipt)
        return 'advanced'
    if reconcile_only:
        return 'no-outstanding-operation'
    item = queue.next()
    if item is None:
        return 'queue-exhausted'
    expected, stage = item
    target = STAGES[STAGES.index(stage)+1]
    op = queue.begin(expected, target, driver.reserve(expected, target))
    driver.apply(op)
    receipt = driver.observe(op)
    if receipt is None:
        return 'awaiting-reconciliation'
    queue.accept(op, receipt)
    return 'advanced'
