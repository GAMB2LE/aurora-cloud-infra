#!/usr/bin/env python3
"""Launch one admitted GWS-first object's guarded copy and reconcile its ledger.

The marker is durable before launch and remains on any uncertain outcome.
This does not establish immutability or authorize retention or deletion.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

from bounded_backlog_runner import Driver, admission, launch, require_reconciled, state, successful, wait_unit
from capture_gws_only_refresh import save
from gws_only_runner import exclusive_json
from gws_pending_replay import require_fresh_source, verified_pending
from prepare_direct_candidate import BASE
from verify_pending_gws_live import main as verify_gws
from gws_object_release import resolve as resolve_object_release, verify_pins, authenticate_worker
from prepare_direct_candidate import run_remote


class FailedObjectWorker(RuntimeError):
    """The recorded object worker reached a terminal failure."""


def main(preparation, *, launch_only=False):
    out = Path(preparation).resolve()
    if out.parent != BASE or not re.fullmatch(r'direct-object-gzip-preparation-20\d{6}T\d{6}Z',out.name):
        raise ValueError('invalid object preparation path')
    fd = os.open(BASE/'bounded-backlog-coordinator.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        require_reconciled(BASE)
        release, argv = resolve_object_release(out)
        verify_pins(release,run_remote)
        previous = json.loads((out/'gws-pending-admission.json').read_bytes())
        request = json.loads((out/'request.json').read_bytes())
        observation = json.loads((out/'observation.json').read_bytes())
        expected = previous['expected']; relative = expected['relative_path']
        rows = [r for r in verified_pending(BASE/'gws-only-pending.sqlite') if r['relative_path'] == relative]
        if len(rows) != 1 or rows[0] != expected: raise ValueError('pending version differs')
        require_fresh_source(expected,request)
        age = time.time()-observation['observed_at']
        if not 0 <= age <= 1200: raise ValueError('object admission is stale')
        operation = request['operation_id']
        if not re.fullmatch(r'20261027-gzip-next-(?:20\d{6}t)?\d{6}z-0',operation):
            raise ValueError('invalid operation')
        unit = 'aurora-object-direct-'+operation+'.service'
        admission_unit = 'aurora-object-gzip-v2-admission-'+out.name.removeprefix('direct-object-gzip-preparation-').lower()+'.service'
        check = admission(admission_unit,request,object_destination=True)
        if check != previous['admission']: raise ValueError('object admission identity differs')
        _, gws = verify_gws(relative)
        if gws.get('expected') != expected or not 0 <= time.time()-observation['observed_at'] <= 1200:
            raise ValueError('GWS version or admission freshness differs')
        db = sqlite3.connect('file:'+str(BASE/'working-ledger.sqlite')+'?mode=ro',uri=True)
        try:
            existing = db.execute('SELECT object_key FROM provisional_object_versions WHERE relative_path=? AND size=? AND mtime_ns=? AND content_sha256=?',
                                  (relative,expected['size'],expected['mtime_ns'],expected['sha256'])).fetchall()
        finally: db.close()
        if existing: raise ValueError('object already recorded; reconcile instead')
        marker = BASE/'gws-pending-object-unresolved.json'
        intent = {'schema':'radar-gws-pending-object-intent-v1','preparation':str(out),'unit':unit,
                  'expected':expected,'admission':check,'object_key_not_yet_known':True,
                  'eviction_authorized':False,'retention_authorized':False}
        exclusive_json(marker,intent)
        identity = launch(unit,out)
        exclusive_json(out/'service-identity.json',identity)
        if launch_only:
            print(json.dumps({'state':'worker-recorded','expected':expected,'identity':identity,
                              'eviction_authorized':False,'retention_authorized':False}))
            return
        props = wait_unit(unit,22500)
        if props.get('InvocationID') != identity['invocation_id'] or props.get('ExecMainPID') != str(identity['main_pid']):
            raise ValueError('object worker identity changed')
        finish(out, marker, intent)
    finally: os.close(fd)

def finish(out, marker, intent, *, resume_import=False):
    expected = intent['expected']
    relative = expected['relative_path']
    operation = intent['unit'].removeprefix('aurora-object-direct-').removesuffix('.service')
    driver = Driver(); driver.obj = {'operation':operation}; driver.obj_out = out
    result = (driver.reconcile_object(relative,resume_import=True)
              if resume_import else driver.reconcile_object(relative))
    if result.get('accepted') is not True: raise ValueError('object ledger reconciliation incomplete')
    finalize_verified_object(out, marker, intent)


def finalize_verified_object(out, marker, intent):
    expected = intent['expected']
    relative = expected['relative_path']
    reconcile = out/'ledger-reconcile'
    events = [json.loads(line) for line in (reconcile/'reconcile.jsonl').read_bytes().splitlines()]
    proof = json.loads((reconcile/'independent-verification.json').read_bytes())
    digest = proof['sha256']
    if (events[-1].get('phase') != 'complete' or events[-1].get('sha256') != digest
            or proof.get('schema') != 'radar-ledger-replica-verification-v1'
            or proof.get('gws', {}).get('readback_verified') is not True
            or proof.get('object', {}).get('readback_verified') is not True
            or any(proof.get(k) is not False for k in ('retention_authorized', 'eviction_authorized'))):
        raise ValueError('object ledger replica evidence differs')
    db = sqlite3.connect('file:'+str(BASE/'working-ledger.sqlite')+'?mode=ro',uri=True)
    try:
        recorded = db.execute('SELECT receipt_sha256,object_key FROM provisional_object_versions WHERE relative_path=? AND size=? AND mtime_ns=? AND content_sha256=?',
                              (relative,expected['size'],expected['mtime_ns'],expected['sha256'])).fetchall()
    finally: db.close()
    if len(recorded) != 1: raise ValueError('reconciled object record missing or ambiguous')
    if (events[0].get('receipt_sha256') != recorded[0][0]
            or not any(e.get('phase') in ('provisional-evidence-imported','prior-import-revalidated')
                       and e.get('object_key') == recorded[0][1] for e in events)):
        raise ValueError('object receipt differs from replica journal')
    completion = {'expected':expected,'receipt_sha256':recorded[0][0],
                   'object_key':recorded[0][1],'snapshot_sha256':digest,
                   'gws_pair_recorded':False,'immutable_backing_proven':False,
                   'eviction_authorized':False,'retention_authorized':False}
    save(out/'gws-pending-object-complete.json', (json.dumps(completion, sort_keys=True)+'\n').encode())
    before = marker.lstat()
    if marker.is_symlink() or json.loads(marker.read_bytes()) != intent: raise ValueError('object guard changed')
    resolved = out/'resolved-object-guard.json'
    try:
        os.link(marker,resolved,follow_symlinks=False)
    except FileExistsError:
        if not os.path.samestat(before, resolved.lstat()):
            raise ValueError('resolved object guard differs')
    after = marker.lstat()
    if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino) or json.loads(marker.read_bytes()) != intent:
        raise ValueError('object guard changed before retirement')
    for directory in (out,BASE):
        if directory == BASE:
            os.unlink(marker)
        descriptor = os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    print(json.dumps({'state':'object-recorded-and-replicated','relative_path':relative,
                      'receipt_sha256':recorded[0][0],'gws_pair_recorded':False,
                      'immutable_backing_proven':False,'eviction_authorized':False,'retention_authorized':False}))


def recover(preparation, expected):
    # Resume observation of the original worker. Never call launch here.
    out = Path(preparation).resolve()
    if out.parent != BASE or not re.fullmatch(r'direct-object-gzip-preparation-20\d{6}T\d{6}Z', out.name):
        raise ValueError('invalid object recovery preparation')
    marker = BASE/'gws-pending-object-unresolved.json'
    identity_path = out/'service-identity.json'
    if not marker.is_file() or not identity_path.is_file():
        return False
    fd = os.open(BASE/'bounded-backlog-coordinator.lock', os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        intent = json.loads(marker.read_bytes())
        identity = json.loads(identity_path.read_bytes())
        if (intent.get('schema') != 'radar-gws-pending-object-intent-v1'
                or intent.get('preparation') != str(out) or intent.get('expected') != expected
                or intent.get('unit') != identity.get('unit')
                or intent.get('retention_authorized') is not False
                or intent.get('eviction_authorized') is not False):
            raise ValueError('object recovery intent differs')
        release, argv = resolve_object_release(out)
        verify_pins(release,run_remote)
        authenticate_worker(intent['unit'],argv,run_remote)
        props = state(intent['unit'])
        if (props.get('InvocationID') != identity['invocation_id']
                or props.get('ExecMainPID') != str(identity['main_pid'])):
            raise RuntimeError('recorded object worker identity no longer matches')
        failed = props.get('SubState') == 'failed' and props.get('Result') == 'exit-code'
        landed = out/'landed-object-readback.json'
        if failed and not landed.is_file():
            raise FailedObjectWorker('recorded object worker failed')
        if not failed and props.get('ActiveState') in ('inactive','failed'):
            raise RuntimeError('recorded object worker stopped without accepted evidence')
        if not (failed or successful(props)):
            return False
        from reconcile_gws_first_pair import check_retention_and_fence
        check_retention_and_fence()
        # Resume a committed import only when no snapshot publication started.
        reconcile_dir = out/'ledger-reconcile'
        resume_import = reconcile_dir.exists()
        if resume_import:
            journal = reconcile_dir/'reconcile.jsonl'
            if not journal.is_file():
                return False
            phases = [json.loads(line).get('phase') for line in journal.read_bytes().splitlines()]
            if phases and phases[-1] == 'complete':
                finalize_verified_object(out, marker, intent)
                return True
            if phases != ['intent','provisional-evidence-imported']:
                return False
        if resume_import:
            finish(out, marker, intent, resume_import=True)
        else:
            finish(out, marker, intent)
        return True
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('preparation')
    parser.add_argument('--launch-only',action='store_true')
    args = parser.parse_args()
    main(args.preparation,launch_only=args.launch_only)
