#!/usr/bin/env python3
"""Record a verified GWS-first pair and replicate its ledger snapshot.

Any failure after intent leaves a guard requiring explicit reconciliation.
This never grants deletion or retention authority.
"""
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from bounded_backlog_runner import require_reconciled
from gws_first_pair import import_pair
from gws_pending_replay import classify_pending, verified_pending
from prepare_direct_candidate import BASE, INFRA, ROOT, run_remote
from verify_object_version_live import main as verify_object
from verify_pending_gws_live import main as verify_gws, selected_endpoint


def save(path, raw):
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f: f.write(raw); f.flush(); os.fsync(f.fileno())
    fd = os.open(Path(path).parent, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def event(path, phase, **details):
    raw = (json.dumps({'phase':phase,'eviction_authorized':False,'retention_authorized':False,**details},sort_keys=True)+'\n').encode()
    fd = os.open(path, os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        while raw:
            n = os.write(fd, raw)
            if n <= 0: raise OSError('short journal write')
            raw = raw[n:]
        os.fsync(fd)
    finally: os.close(fd)


def load_ledger():
    path = INFRA/'roles/radar_archive_cache/files/aurora-radar-archive-ledger.py'
    spec = importlib.util.spec_from_file_location('pair_ledger', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def check_retention_and_fence():
    hold = run_remote(['sudo', '-n', 'sha256sum', '/etc/aurora/ass-retention.hold'], timeout=60)
    if hold.stdout.decode().split()[0] != '22dce428efe03586a4cdf751c327a9c34e685922a9907c8b193d5d684cab3e24':
        raise ValueError('retention hold differs')
    fence = run_remote(['sudo', '-n', 'python3', '-c',
                        'import os; print(int(os.path.lexists("/etc/aurora/gws-direct-publication.fence")))'], timeout=60)
    if fence.stdout.strip() != b'0': raise ValueError('GWS publication fence remains')


def main(relative):
    lock = os.open(BASE/'bounded-backlog-coordinator.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        require_reconciled(BASE)
        pending = [r for r in verified_pending(BASE/'gws-only-pending.sqlite') if r['relative_path'] == relative]
        if len(pending) != 1: raise ValueError('pending version missing or ambiguous')
        expected = pending[0]
        # Recheck under the pair lock; the earlier coordinator check alone
        # cannot exclude another actor completing the pair in the interim.
        classified = [r for r in classify_pending(BASE/'gws-only-pending.sqlite', BASE/'working-ledger.sqlite')
                      if r['expected'] == expected]
        if len(classified) != 1 or classified[0]['status'] != 'pair-reconciliation-needed':
            raise ValueError('pair ledger status requires observation or reconciliation')
        db = sqlite3.connect('file:'+str(BASE/'working-ledger.sqlite')+'?mode=ro', uri=True)
        try:
            rows = db.execute('SELECT receipt_sha256,writer_sha256 FROM provisional_object_versions WHERE relative_path=? AND size=? AND mtime_ns=? AND content_sha256=?',
                              (relative, expected['size'], expected['mtime_ns'], expected['sha256'])).fetchall()
        finally: db.close()
        if len(rows) != 1: raise ValueError('exact object receipt missing or ambiguous')
        receipt, writer_sha = rows[0]
        writer = (ROOT/'tmp/radar-direct-archive-20261105/direct_object_gzip_recovery.py').read_bytes()
        if hashlib.sha256(writer).hexdigest() != writer_sha: raise ValueError('object writer pin differs')
        pin = run_remote(['sudo','-n','sha256sum','/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/direct-object-gzip-supervisor-v2/direct_object_gzip_recovery.py']).stdout.decode().split()[0]
        if pin != writer_sha: raise ValueError('deployed object writer pin differs')
        obj_path, obj = verify_object(receipt)
        try:
            gws_path, gws = verify_gws(relative)
        except subprocess.CalledProcessError as error:
            # This exception originates in the read-only GWS check, before
            # marker creation. Preserve nested stderr for exact classification.
            raw = error.stderr or b''
            if isinstance(raw, bytes): raw = raw[-32768:].decode(errors='replace')
            diagnostic = {'phase':'gws-readback','returncode':error.returncode,'stderr':raw[-32768:], 'endpoint':selected_endpoint()}
            print('PAIR_GWS_READBACK_FAILURE '+json.dumps(diagnostic,sort_keys=True),file=sys.stderr,flush=True)
            raise
        check_retention_and_fence()
        now = datetime.datetime.now(datetime.timezone.utc)
        out = BASE/('gws-first-pair-reconcile-'+now.strftime('%Y%m%dT%H%M%SZ')); out.mkdir(mode=0o700)
        journal = out/'journal.jsonl'
        marker = BASE/'gws-first-pair-unresolved.json'
        intent = {'relative_path':relative,'receipt_sha256':receipt,'object_evidence':str(obj_path),
                  'gws_evidence':str(gws_path),'reconcile_directory':str(out),
                  'eviction_authorized':False,'retention_authorized':False}
        marker_raw = (json.dumps(intent,sort_keys=True)+'\n').encode()
        save(marker, marker_raw)
        event(journal, 'intent', **intent)
        ledger = load_ledger(); connection = ledger.connect(BASE/'working-ledger.sqlite')
        try:
            result = import_pair(connection, expected, receipt, writer, gws, obj, now=now)
            event(journal, 'pair-imported', receipt_sha256=result['receipt_sha256'])
            snap = ledger.snapshot_database(connection, BASE/'ledger-snapshots')
        finally: connection.close()
        path = Path(snap['path']); sha = snap['snapshot_sha256']; event(journal, 'snapshot-created', sha256=sha)
        for name, command in [('primary',[sys.executable,str(INFRA/'scripts/install_radar_snapshot_primary.py'),str(path),'--sha256',sha]),
                              ('archives',[sys.executable,str(INFRA/'scripts/publish_radar_ledger_snapshot.py'),str(path),'--sha256',sha,'--replicate-object-unprotected'])]:
            event(journal, name+'-intent', sha256=sha)
            proc = subprocess.run(command,capture_output=True,timeout=360)
            save(out/(name+'-stdout.json'),proc.stdout); save(out/(name+'-stderr.txt'),proc.stderr)
            if proc.returncode: raise RuntimeError(name+' publication outcome requires reconciliation')
            proof = json.loads(proc.stdout)
            if proof.get('sha256') != sha or proof.get('size') != path.stat().st_size:
                raise ValueError(name+' snapshot identity differs')
            if name == 'primary':
                if proof.get('atomic_no_replace') is not True or proof.get('read_only_integrity_check') != 'ok':
                    raise ValueError('primary snapshot proof differs')
            else:
                stores = proof.get('stores', {})
                if proof.get('schema') != 'radar-ledger-snapshot-publication-v1' or proof.get('eviction_authorized') is not False or any(
                    stores.get(store, {}).get('verified') is not True or stores[store].get('sha256') != sha
                    or stores[store].get('size') != path.stat().st_size for store in ('gws', 'object')):
                    raise ValueError('archive snapshot proof differs')
            event(journal, name+'-complete', sha256=sha)
        object_path = json.loads((out/'archives-stdout.json').read_bytes())['stores']['object']['path']
        command = [sys.executable,str(INFRA/'scripts/verify_radar_snapshot_replica.py'),str(path),'--sha256',sha,'--replica',object_path]
        proc = subprocess.run(command,capture_output=True,timeout=360)
        save(out/'independent-verification.json',proc.stdout); save(out/'independent-verification-stderr.txt',proc.stderr)
        if proc.returncode: raise RuntimeError('snapshot verification requires reconciliation')
        proof = json.loads(proc.stdout)
        if (proof.get('schema') != 'radar-ledger-replica-verification-v1' or proof.get('sha256') != sha
            or proof.get('size') != path.stat().st_size or proof.get('gws', {}).get('readback_verified') is not True
            or proof.get('object', {}).get('path') != object_path or proof['object'].get('readback_verified') is not True
            or proof.get('eviction_authorized') is not False or proof.get('retention_authorized') is not False):
            raise ValueError('independent snapshot proof differs')
        check_retention_and_fence()
        event(journal, 'complete', sha256=sha)
        archived = out/'resolved-marker.json'
        before = marker.lstat()
        if marker.is_symlink() or marker.read_bytes() != marker_raw: raise ValueError('pair marker changed')
        os.link(marker,archived,follow_symlinks=False)
        after = marker.lstat()
        if (before.st_dev,before.st_ino) != (after.st_dev,after.st_ino) or marker.read_bytes() != marker_raw:
            raise ValueError('pair marker changed before retirement')
        os.unlink(marker)
        for directory in (out, BASE):
            fd = os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try: os.fsync(fd)
            finally: os.close(fd)
        print(json.dumps({'state':'provisional-pair-replicated','relative_path':relative,'snapshot_sha256':sha,
                          'immutable_backing_proven':False,'eviction_authorized':False,'retention_authorized':False}))
    finally: os.close(lock)


if __name__ == '__main__': main(sys.argv[1])
