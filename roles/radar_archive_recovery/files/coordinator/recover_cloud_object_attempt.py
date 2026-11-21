#!/usr/bin/env python3
"""Reconcile an exact failed cloud upload only after two clean absence probes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from bounded_backlog_runner import capacity_refusal, state
from capture_gws_only_refresh import save
from inspect_uncertain_object import REMOTE_CODE
from per_file_queue import AUTHORITY, Queue, writer_lock
from prepare_direct_candidate import BASE, run_remote
from reconcile_gws_first_pair import check_retention_and_fence
from verify_pending_gws_live import main as verify_gws
from verify_object_version_live import read_exact
from gws_object_release import resolve as resolve_object_release, verify_pins, authenticate_worker

REMOTE = '/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/direct-object-v1'
NAMESPACE = 'data/internal/aurora-cloud/radar-archive-first/versions/v1/'
MARKER = BASE/'gws-pending-object-unresolved.json'


class ObjectStoreUnavailable(RuntimeError):
    """An authenticated object probe could not determine the outcome."""


class VerifiedLandedObject(RuntimeError):
    """Independent bytes exist for the original key and need ledger import."""


def object_state(result, key, prefix):
    checks = result['checks']
    if (result['key'] != key or result['prefix'] != prefix or len(checks) != 2
            or not all(item.get('control_http') == 200 and item.get('control_size') == 3428352
                       and item.get('list_http') == 200 and item.get('list_truncated') is False
                       and item.get('multipart_http') == 200 and item.get('multipart_truncated') is False
                       and item.get('uploads') == [] for item in checks)):
        raise ValueError('object or multipart state requires reconciliation')
    if all(item.get('target_http') == 404 and item.get('objects') == [] for item in checks):
        return 'absent'
    if all(item.get('target_http') == 200 and item.get('objects') == [key] for item in checks):
        return 'present'
    if any(item.get('target_http') in (429, 500, 502, 503, 504) for item in checks):
        raise ObjectStoreUnavailable('object HEAD temporarily unavailable')
    raise ValueError('object listings disagree with exact key observation')


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def signature(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def fetch_root(path):
    return run_remote(['sudo','-n','cat',path]).stdout


def probe_object(key, prefix):
    code = REMOTE_CODE.replace(
        NAMESPACE+'1e537758958a82f9a2cb0273936d99a7/',prefix,
    ).replace('d6acfa8a819096da7687c15cdc97f206d71692c41054f7f9b3596ba989bf87a1.radar',
              key.rsplit('/',1)[1]).replace(
        ';c=_client(config)',
        ';config.update(recovery_s3_connect_timeout_seconds=5,recovery_s3_read_timeout_seconds=8);c=_client(config)',
    )
    code = f'''from botocore.exceptions import (ClientError, ConnectTimeoutError, ReadTimeoutError,
                                 EndpointConnectionError, ConnectionClosedError)
try:
    exec({code!r})
except (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError, ConnectionClosedError):
    raise SystemExit(75) from None
except ClientError as exc:
    status = exc.response.get('ResponseMetadata', {{}}).get('HTTPStatusCode', 0)
    if status == 429 or 500 <= status < 600:
        raise SystemExit(75) from None
    raise
'''
    try:
        response = run_remote(
            ['sudo','-n','-u','aurora','--','timeout','75s',
             '/opt/aurora-object-store-recovery/venv/bin/python','-c',code],timeout=85)
    except subprocess.CalledProcessError as error:
        if error.returncode in (75, 124):
            raise ObjectStoreUnavailable('object absence probe unavailable') from None
        raise
    except subprocess.TimeoutExpired:
        raise ObjectStoreUnavailable('object absence probe timed out') from None
    result = json.loads(response.stdout)
    return object_state(result, key, prefix), result


def release_guard(quarantine, evidence):
    if (not stat.S_ISREG(quarantine.lstat().st_mode)
            or hashlib.sha256(quarantine.read_bytes()).hexdigest() != evidence['guard_sha256']):
        raise ValueError('quarantined guard differs from committed receipt')
    if os.path.lexists(MARKER):
        if signature(MARKER.lstat()) != signature(quarantine.lstat()):
            raise ValueError('unresolved guard does not match the quarantine')
        MARKER.unlink()
    sync_directory(BASE)


def recover(queue, operation_id, prep):
    if prep.parent != BASE or not re.fullmatch(r'direct-object-gzip-preparation-20\d{6}T\d{6}Z', prep.name):
        raise ValueError('unexpected preparation path')
    quarantine = prep/'quarantined-object-guard.json'
    check_retention_and_fence()
    op = queue.outstanding()
    if op is None:
        receipt = queue.finished_receipt(operation_id)
        if (not receipt or receipt.get('schema') != 'radar-absent-object-attempt-v1'
                or receipt.get('preparation') != str(prep)
                or any(receipt.get(k) is not v for k,v in AUTHORITY.items())):
            raise ValueError('no committed reconciliation for this operation')
        release_guard(quarantine, receipt)
        return receipt['failed_key']
    if (op['id'] != operation_id or op['target'] != 'object-verified'
            or op['intent'].get('preparation') != str(prep)):
        raise ValueError('different operation is outstanding')
    expected = op['expected']
    before = MARKER.lstat()
    raw = MARKER.read_bytes()
    guard = json.loads(raw)
    request = json.loads((prep/'request.json').read_bytes())
    identity = json.loads((prep/'service-identity.json').read_bytes())
    operation = request['operation_id']
    source = request['expected_source']
    if (request['relative_path'] != expected['relative_path'] or source['sha256'] != expected['sha256']
            or source['identity'][2:4] != [expected['size'], expected['mtime_ns']]):
        raise ValueError('recorded upload source differs')
    if not re.fullmatch(r'20261027-gzip-next-(?:20\d{6}t)?\d{6}z-0',operation):
        raise ValueError('invalid recorded operation')
    unit = 'aurora-object-direct-'+operation+'.service'
    release, argv = resolve_object_release(prep)
    verify_pins(release,run_remote)
    authenticate_worker(unit,argv,run_remote)
    terminal = state(unit)
    group = run_remote(['systemctl','show',unit,'-p','ControlGroup','--value']).stdout.strip()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink not in (1, 2)
            or (before.st_nlink == 2 and signature(quarantine.lstat()) != signature(before))
            or guard.get('preparation') != str(prep) or guard.get('expected') != expected
            or guard.get('unit') != unit or identity.get('unit') != unit
            or terminal.get('SubState') != 'failed' or terminal.get('Result') != 'exit-code'
            or terminal.get('InvocationID') != identity['invocation_id']
            or terminal.get('ExecMainPID') != str(identity['main_pid']) or group
            or any(guard.get(k) is not v for k,v in AUTHORITY.items())
            or (prep/'ledger-reconcile').exists() or (prep/'gws-pending-object-complete.json').exists()):
        raise ValueError('failed worker or guard identity differs')
    stem = REMOTE+'/transfer-'+operation+'.jsonl'
    observed = run_remote(['sudo','-n','python3','-c',
                           'import json,os,sys;print(json.dumps([os.path.lexists(p) for p in sys.argv[1:]]))',
                           stem+'.preflight.json', stem, stem+'.readback.json']).stdout
    artifacts = json.loads(observed)

    def retire(evidence):
        check_retention_and_fence()
        if (state(unit) != terminal or MARKER.read_bytes() != raw
                or signature(MARKER.lstat()) != signature(before)):
            raise ValueError('worker or guard changed before retirement')
        if before.st_nlink == 1:
            os.link(MARKER, quarantine, follow_symlinks=False)
        sync_directory(prep)
        queue.retire_absent_object_attempt(op,evidence)
        release_guard(quarantine,evidence)
        return evidence['failed_key']

    if artifacts == [False, False, False]:
        blocker = capacity_refusal(unit, terminal)
        if not blocker or guard.get('object_key_not_yet_known') is not True:
            raise ValueError('no-stream outcome lacks a confirmed capacity refusal')
        evidence = {'schema':'radar-absent-object-attempt-v1','operation_id':op['id'],'expected':expected,
                    'preparation':str(prep),'object_absent':True,'multipart_absent':True,
                    'failed_key_reuse_authorized':False,'worker':terminal,'failed_key':None,
                    'checks':{'capacity_refusal':blocker,'upload_journal_created':False},
                    'guard_sha256':hashlib.sha256(raw).hexdigest(),**AUTHORITY}
        return retire(evidence)
    if artifacts != [True, True, False]:
        raise ValueError('object transfer artifacts require reconciliation')
    preflight, journal = fetch_root(stem+'.preflight.json'), fetch_root(stem)
    pre = json.loads(preflight)
    events = [json.loads(line) for line in journal.splitlines()]
    nonce = pre['operation']['nonce']
    if not re.fullmatch(r'[0-9a-f]{32}',nonce):
        raise ValueError('failed key nonce invalid')
    prefix = NAMESPACE+nonce+'/'
    key = prefix+expected['sha256']+'.radar'
    remote_key = 'gamb2le-object-store:gamb2le-o/'+key
    if (pre['expected'] != expected or pre['operation']['object_key'] != remote_key
            or [row['phase'] for row in events] != ['intent','outcome-uncertain']
            or events[0]['operation'] != pre['operation'] or events[1]['object_key'] != remote_key
            or events[1]['source_exit'] != 0 or events[1]['destination_exit'] == 0
            or any(row.get(k) != v for row in events for k,v in expected.items())
            or any(row.get(k) is not v for row in [pre,*events] for k,v in AUTHORITY.items())):
        raise ValueError('failed stream evidence differs')
    outcome, proof = probe_object(key,prefix)
    gws_path,gws = verify_gws(expected['relative_path'])
    if gws['expected'] != expected:
        raise ValueError('GWS source version differs')
    check_retention_and_fence()
    if (state(unit) != terminal or MARKER.read_bytes() != raw
            or signature(MARKER.lstat()) != signature(before)):
        raise ValueError('worker or guard changed during reconciliation')
    if outcome == 'present':
        try:
            readback = read_exact(expected, remote_key)
        except subprocess.TimeoutExpired:
            raise ObjectStoreUnavailable('landed object readback timed out') from None
        except subprocess.CalledProcessError as error:
            if re.search(rb'\b(?:ReadTimeoutError|ConnectTimeoutError|EndpointConnectionError|ConnectionClosedError)\b',
                         error.stderr or b''):
                raise ObjectStoreUnavailable('landed object readback unavailable') from None
            raise
        check_retention_and_fence()
        if state(unit) != terminal or MARKER.read_bytes() != raw or signature(MARKER.lstat()) != signature(before):
            raise ValueError('worker or guard changed during landed object readback')
        path = prep/'landed-object-readback.json'
        record = {'schema':'radar-landed-object-recovery-v1','operation_id':op['id'],
                  'readback':readback,'worker':terminal,
                  'preflight_sha256':hashlib.sha256(preflight).hexdigest(),
                  'journal_sha256':hashlib.sha256(journal).hexdigest(),
                  'guard_sha256':hashlib.sha256(raw).hexdigest(),**AUTHORITY}
        if path.exists():
            stored = json.loads(path.read_bytes())
            record['readback']['checked_at'] = stored['readback']['checked_at']
        save(path, (json.dumps(record, sort_keys=True)+'\n').encode())
        raise VerifiedLandedObject('landed object independently verified; ledger import required')
    evidence = {'schema':'radar-absent-object-attempt-v1','operation_id':op['id'],'expected':expected,
                'preparation':str(prep),'object_absent':True,'multipart_absent':True,
                'failed_key_reuse_authorized':False,
                'worker':terminal,'failed_key':remote_key,'checks':proof,'gws_evidence':str(gws_path),
                'preflight_sha256':hashlib.sha256(preflight).hexdigest(),
                'journal_sha256':hashlib.sha256(journal).hexdigest(),
                'guard_sha256':hashlib.sha256(raw).hexdigest(),**AUTHORITY}
    return retire(evidence)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation_id')
    parser.add_argument('preparation', type=Path)
    args = parser.parse_args()
    with writer_lock(BASE/'per-file-coordinator.lock'),writer_lock(BASE/'bounded-backlog-coordinator.lock'):
        if state('aurora-radar-coordinator.service')['SubState'] != 'failed':
            raise SystemExit('recovery requires the stopped coordinator')
        queue=Queue(BASE/'per-file-archive-queue.sqlite')
        try:
            key = recover(queue, args.operation_id, args.preparation.resolve())
            print(json.dumps({'reconciled':args.operation_id,'quarantined':key,**AUTHORITY},sort_keys=True))
        except ObjectStoreUnavailable as error:
            print(f'{error}. Guard remains in place.', flush=True)
            raise SystemExit(75) from None
        finally: queue.close()
