#!/usr/bin/env python3
"""Publish one settled native file through the existing guarded GWS worker.

The queue is separate from the frozen cohort. The old supervisor must be held
off at the systemd level while both queues share the pending-evidence ledger.
This canary only advances through GWS verification; it grants no deletion or
retention authority and does not claim object replication.
"""
import contextlib
import json
import os
from pathlib import Path
import stat
import subprocess

import per_file_coordinator as coordinator
from per_file_queue import Queue, advance, writer_lock
import pair_retry

BASE = coordinator.BASE
QUEUE = BASE / 'fresh-gws-canary.sqlite'
HOLD = Path('/etc/aurora/radar-frozen-coordinator.hold')
DROPIN = Path('/etc/systemd/system/aurora-radar-coordinator.service.d/99-fresh-arrival-hold.conf')
DROPIN_CONTENT = b'[Unit]\nConditionPathExists=!/etc/aurora/radar-frozen-coordinator.hold\n'


def require_supervisor_held():
    for path in (HOLD, DROPIN):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('supervisor hold or drop-in is not protected')
    if DROPIN.read_bytes() != DROPIN_CONTENT:
        raise ValueError('supervisor hold drop-in differs')
    values = subprocess.run(
        ['systemctl', 'show', 'aurora-radar-coordinator.service',
         '--property=ActiveState', '--property=DropInPaths', '--property=NeedDaemonReload'],
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout
    fields = dict(line.split('=', 1) for line in values.splitlines() if '=' in line)
    if fields.get('ActiveState') not in {'inactive', 'failed'}:
        raise ValueError('frozen supervisor is not stopped')
    if (str(DROPIN) not in fields.get('DropInPaths', '').split()
            or fields.get('NeedDaemonReload') != 'no'):
        raise ValueError('supervisor hold is not loaded by systemd')
    condition = subprocess.run(
        ['systemd-analyze', 'condition', 'ConditionPathExists=!' + str(HOLD)],
        capture_output=True, text=True, timeout=15,
    )
    if condition.returncode != 1 or 'Conditions failed.' not in condition.stdout + condition.stderr:
        raise ValueError('supervisor hold condition did not fail closed')


def start(candidate):
    require_supervisor_held()
    with writer_lock(BASE / 'per-file-coordinator.lock'), contextlib.closing(Queue(QUEUE)) as queue:
        with contextlib.closing(Queue(coordinator.STATE, readonly=True)) as frozen:
            if frozen.outstanding() is not None:
                raise ValueError('frozen cohort has an unresolved operation')
        if queue.outstanding() is not None:
            raise ValueError('canary queue has unresolved work; reconcile it first')
        existing = [(json.loads(row['identity']), row['stage']) for row in
                    queue.db.execute('SELECT identity, stage FROM files')]
        matching = [(identity, stage) for identity, stage in existing
                    if identity['relative_path'] == candidate['relative_path']]
        if len(matching) > 1 or (matching and matching[0][1] != 0):
            raise ValueError('canary path already has a recorded version')
        with writer_lock(BASE / 'bounded-backlog-coordinator.lock'):
            # Count all GWS-only versions while both archive writer locks are
            # held; otherwise another writer could invalidate the admission.
            global_pending = [row['expected'] for row in coordinator.classify_pending(
                coordinator.PENDING, coordinator.LEDGER) if row['status'] == 'object-copy-needed']
            if any(identity['relative_path'] == candidate['relative_path'] for identity in global_pending):
                raise ValueError('canary path already has a pending GWS version')
            identities = global_pending + [identity for identity, stage in existing
                                          if stage < 2 and identity['relative_path'] != candidate['relative_path']]
            backlog = {json.dumps(identity, sort_keys=True): identity for identity in identities}.values()
            if (type(candidate.get('size')) is not int or candidate['size'] <= 0
                    or len(backlog) + 1 > 256
                    or sum(identity['size'] for identity in backlog) + candidate['size'] > 16 * 1024**3):
                raise ValueError('GWS-only backlog capacity would be exceeded')
            coordinator.require_reconciled(BASE)
            coordinator.check_retention_and_fence()
            prepared, expected = coordinator.prepare_one(candidate, long_gws=True)
            if matching and matching[0][0] != expected:
                raise ValueError('previously queued canary version differs')
            queue.add(expected)
            driver = coordinator.Driver(prepared)
            operation = queue.begin(expected, 'gws-verified', driver.reserve(expected, 'gws-verified'))
            driver.apply(operation)
            receipt = driver.observe(operation)
            if receipt is None:
                result = 'awaiting-reconciliation'
            else:
                queue.accept(operation, receipt)
                result = 'advanced'
        return {'result': result, **queue.status()}


def reconcile():
    require_supervisor_held()
    with writer_lock(BASE / 'per-file-coordinator.lock'), contextlib.closing(Queue(QUEUE)) as queue:
        operation = queue.outstanding()
        if operation is None or operation['target'] != 'gws-verified':
            raise ValueError('no GWS canary operation needs reconciliation')
        result = coordinator.step(queue, None, reconcile_only=True)
        return {'result': result, **queue.status()}


def replicate():
    """Advance or reconcile one canary's existing object/pair/checkpoint stage."""
    require_supervisor_held()
    # Pair validation must authenticate the operation against this queue,
    # never against the frozen cohort queue.
    pair_retry.STATE = QUEUE
    with writer_lock(BASE / 'per-file-coordinator.lock'), contextlib.closing(Queue(QUEUE)) as queue:
        operation = queue.outstanding()
        if operation is not None:
            if operation['target'] == 'gws-verified':
                raise ValueError('reconcile the active GWS canary first')
            result = coordinator.step(queue, None, reconcile_only=True)
        else:
            coordinator.require_reconciled(BASE)
            coordinator.check_retention_and_fence()
            item = queue.next()
            if item is not None and item[1] == 'discovered':
                raise ValueError('canary requires GWS verification before replication')
            result = advance(queue, coordinator.Driver())
        return {'result': result, **queue.status()}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'reconcile', 'replicate', 'status'))
    parser.add_argument('--relative-path')
    parser.add_argument('--size', type=int)
    parser.add_argument('--mtime', type=int)
    args = parser.parse_args()
    if args.action == 'status':
        with contextlib.closing(Queue(QUEUE, readonly=True)) as queue:
            result = queue.status()
    elif args.action == 'reconcile':
        result = reconcile()
    elif args.action == 'replicate':
        result = replicate()
    else:
        if not args.relative_path or not args.size or not args.mtime:
            parser.error('start requires relative path, size, and mtime')
        result = start({'relative_path': args.relative_path, 'size': args.size, 'mtime': args.mtime})
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
