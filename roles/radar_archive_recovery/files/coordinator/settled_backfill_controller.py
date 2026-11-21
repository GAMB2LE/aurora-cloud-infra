"""Start or reconcile one settled native GWS backfill under archive locks.

The frozen cohort remains in its own queue. A durable outstanding operation in
the shared fresh queue makes the historical coordinator wait until it resolves.
This controller never authorizes eviction or claims object replication.
"""
import contextlib
import json
import os
from pathlib import Path
import runpy
import shlex
import time

import gws_only_runner
from primary_dispatch_delegation import delegated as primary_dispatch_delegated, active_repair_owned
from finalized_fresh_controller import FRESH, archive_locks, require_r7
from gws_object_isolation import check as check_isolation
from gws_object_release import resolve as resolve_object_release
from gws_pending_replay import classify_pending, valid_expected
from per_file_coordinator import (BASE, LEDGER, PENDING, STATE, Driver, check_retention_and_fence,
                                  load_candidates, prepare_one, require_reconciled)
from per_file_queue import Queue
from prepare_gws_only_candidate_refresh import INFRA, run_remote

MAX_PENDING_FILES = 256
MAX_PENDING_BYTES = 16 * 1024**3


def candidate_version(candidate):
    code = runpy.run_path(str(INFRA/'scripts/aurora_radar_bounded_canary.py'))['source_code']()
    remote = ['sudo','-n','-u','aurora','ssh','-o','BatchMode=yes','-o','ConnectTimeout=12',
              '-o','IdentitiesOnly=yes','-o','IdentityFile=none','-o','PubkeyAuthentication=no',
              '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile=/home/aurora/.ssh/known_hosts',
              'aurora@100.124.55.22', shlex.join(['python3','-c',code,
              '/home/aurora/data/rpgfmcw94/'+candidate['relative_path']])]
    source = json.loads(run_remote(remote, timeout=1800).stdout)
    identity = source.get('identity', [])
    if (not isinstance(identity, list) or len(identity) != 10 or identity[2] != candidate['size']
            or identity[3]//10**9 != candidate['mtime']):
        raise ValueError('candidate source changed since inventory')
    expected = {'relative_path': candidate['relative_path'], 'size': identity[2],
                'mtime_ns': identity[3], 'sha256': source.get('sha256')}
    if not valid_expected(expected): raise ValueError('candidate source proof is invalid')
    return expected


def start(manifest, digest):
    os.umask(0o077)
    require_r7()
    candidates = load_candidates(Path(manifest), digest)
    with archive_locks() as fds, contextlib.closing(Queue(FRESH)) as fresh, contextlib.closing(Queue(STATE, readonly=True)) as frozen:
        require_r7()
        other = frozen.outstanding()
        if fresh.outstanding() is not None or (other is not None and other.get('target') != 'object-verified'):
            raise ValueError('an archive operation requires reconciliation')
        if other is None:
            require_reconciled(BASE); check_retention_and_fence()
            known = gws_only_runner.known_paths()
        else:
            if os.environ.get('RADAR_GWS_WORKER_RELEASE') != 'gzip-v5-vm01-lanes':
                raise ValueError('concurrent backfill requires the lane-aware GWS release')
            release, _ = resolve_object_release(Path(other['intent']['preparation']))
            if release.get('id') != 'gws-object-vm01-lanes':
                raise ValueError('concurrent object worker is not lane-aware')
            known = set()
        for queue in (fresh, frozen):
            known.update(json.loads(row[0])['relative_path'] for row in queue.db.execute('SELECT identity FROM files'))
        known.update(primary_dispatch_delegated(candidates))
        known.update(active_repair_owned(candidates))
        inventory = classify_pending(PENDING, LEDGER)
        if any(not valid_expected(row.get('expected')) or row.get('status') not in
               {'object-copy-needed', 'pair-reconciliation-needed', 'provisional-pair-recorded'} for row in inventory):
            raise ValueError('global pending inventory is invalid')
        pending = [row['expected'] for row in inventory if row['status'] != 'provisional-pair-recorded']
        count, used = len(pending), sum(item['size'] for item in pending)
        def choose():
            return next((row for row in candidates if row['relative_path'] not in known
                         and row['relative_path'] not in {item['relative_path'] for item in pending}
                         and type(row.get('mtime')) is int and time.time()-row['mtime'] >= 21600
                         and type(row.get('size')) is int and 0 < row['size']
                         and count + 1 <= MAX_PENDING_FILES and used + row['size'] <= MAX_PENDING_BYTES), None)
        chosen = choose()
        if chosen is None:
            return {'state': 'no-admissible-candidate', 'retention_authorized': False, 'eviction_authorized': False}
        if other is not None:
            observed = candidate_version(chosen)
            proof = check_isolation(observed, other, lock_fds=fds, base=BASE)
            known.update(gws_only_runner.known_paths(isolated_candidate=observed, object_operation=other, lock_fds=fds))
            if observed['relative_path'] in known:
                chosen = choose()
                if chosen is None:
                    return {'state': 'no-admissible-candidate', 'retention_authorized': False, 'eviction_authorized': False}
                observed = candidate_version(chosen)
                proof = check_isolation(observed, other, lock_fds=fds, base=BASE)
        prep, expected = prepare_one(chosen, long_gws=True)
        if other is not None and (expected != observed or check_isolation(expected, other, lock_fds=fds, base=BASE) != proof):
            raise ValueError('candidate or object isolation changed during admission')
        fresh.add(expected)
        driver = Driver(prep)
        operation = fresh.begin(expected, 'gws-verified', driver.reserve(expected, 'gws-verified'))
        driver.apply(operation)
        return {'state': 'started', 'operation': operation, 'retention_authorized': False, 'eviction_authorized': False}


def reconcile():
    os.umask(0o077)
    require_r7()
    with archive_locks(), contextlib.closing(Queue(FRESH)) as fresh:
        operation = fresh.outstanding()
        if operation is None:
            return {'state': 'no-outstanding-operation', 'retention_authorized': False, 'eviction_authorized': False}
        out = Path(operation['intent']['preparation']['out'])
        if 'finalized_stage' in json.loads((out/'request.json').read_bytes()):
            raise ValueError('finalized operation requires its own reconciler')
        receipt = Driver().observe(operation)
        if receipt is None:
            return {'state': 'awaiting-reconciliation', 'operation': operation,
                    'retention_authorized': False, 'eviction_authorized': False}
        fresh.accept(operation, receipt)
        return {'state': 'gws-verified', 'operation': operation, 'retention_authorized': False, 'eviction_authorized': False}
