#!/usr/bin/env python3
"""Serial coordinator over the existing guarded GWS and object workers."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import gws_only_runner
from gws_worker_release import resolve_preparation
from apply_pending_object import FailedObjectWorker, recover as recover_object
from bounded_backlog_runner import AdmissionCapacityUnavailable, admission, require_reconciled, state, successful
from capture_gws_only_refresh import capture, save
from gws_pending_ledger import register_captured
from gws_pending_replay import classify_pending, valid_expected, verified_pending
from per_file_queue import AUTHORITY, Queue, advance, writer_lock
from prepare_direct_candidate import BASE
from prepare_gws_only_candidate_refresh import prepare
from recover_abandoned_gws_stage import recover as reconcile_partial_gws
from recover_cloud_gws_stage import GwsCapacityUnavailable, IncompleteGwsStage, recover as reconcile_failed_gws
from recover_cloud_object_attempt import MARKER as OBJECT_GUARD, ObjectStoreUnavailable, VerifiedLandedObject
from recover_cloud_object_attempt import recover as reconcile_failed_object
from reconcile_gws_first_pair import check_retention_and_fence
from resume_pending_object import invoke
from pair_retry import run_once as run_pair_once, retry_under_lock as retry_pair_under_lock, PairVerificationUnavailable

HERE = Path(__file__).resolve().parent
PENDING = BASE/'gws-only-pending.sqlite'
LEDGER = BASE/'working-ledger.sqlite'
STATE = BASE/'per-file-archive-queue.sqlite'
STATUS = BASE/'per-file-status.json'


class FailedGwsWorker(RuntimeError):
    """The recorded GWS worker reached a terminal failure."""


def publish_status(value):
    with tempfile.NamedTemporaryFile(mode='w', dir=BASE, prefix='.per-file-status-',
                                     delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, STATUS)
            fd = os.open(BASE, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)


def read_json(path):
    return json.loads(path.read_bytes())


def candidate_identity(preparation):
    request = read_json(preparation/'request.json')
    source = request['expected_source']
    expected = {'relative_path':request['relative_path'], 'size':source['identity'][2],
                'mtime_ns':source['identity'][3], 'sha256':source['sha256']}
    if not valid_expected(expected):
        raise ValueError('invalid admitted file version')
    return expected


def import_pending(queue):
    # Idempotent entry. The GWS stage independently validates the stored
    # readback bundle before issuing a stage receipt.
    for row in classify_pending(PENDING, LEDGER):
        if row['status'] != 'provisional-pair-recorded':
            queue.add(row['expected'])


class Driver:
    def __init__(self, prepared=None):
        self.prepared = prepared

    def reserve(self, expected, target):
        relative = expected['relative_path']
        if target == 'gws-verified' and self.prepared:
            prep = self.prepared
            if candidate_identity(Path(prep['out'])) != expected:
                raise ValueError('GWS preparation differs from queued version')
            return {'preparation':prep, 'unit':'aurora-direct-'+prep['operation']+'.service'}
        if target == 'object-verified':
            current = [row for row in classify_pending(PENDING, LEDGER) if row['expected'] == expected]
            if len(current) != 1:
                raise ValueError('GWS pending version missing')
            if current[0]['status'] != 'object-copy-needed':
                return {'reuse_existing_object':True}
            result = subprocess.run(
                [sys.executable, str(HERE/'prepare_pending_object.py'), relative, '--from-gws'],
                capture_output=True, text=True, timeout=1800,
            )
            if result.returncode == 75:
                refusal = json.loads(result.stdout.splitlines()[-1])
                blocker = refusal.get('blocker', '')
                if (refusal.get('transfer_started') is not False
                        or blocker not in {f'direct-transfer capacity insufficient: {path}'
                                           for path in ('/', '/data', '/project')}):
                    raise ValueError('invalid object capacity refusal')
                raise AdmissionCapacityUnavailable(blocker)
            result.check_returncode()
            admitted = json.loads(result.stdout.splitlines()[-1])
            if admitted['expected'] != expected or admitted['transfer_started'] is not False:
                raise ValueError('object admission differs')
            return {'preparation':admitted['preparation']}
        return {'relative_path':relative}

    def apply(self, op):
        target, intent = op['target'], op['intent']
        path = op['expected']['relative_path']
        if target == 'gws-verified' and 'preparation' in intent:
            prepared = intent['preparation']
            resolve_preparation(Path(prepared['out']), intent_preparation=prepared)
            worker = gws_only_runner.Driver()
            worker.prepared = prepared
            worker.out = Path(prepared['out'])
            worker.request = read_json(worker.out/'request.json')
            worker.transfer(path, launch_only=True)
        elif target == 'object-verified' and 'preparation' in intent:
            result = subprocess.run(
                [sys.executable, str(HERE/'apply_pending_object.py'),
                 intent['preparation'], '--launch-only'],
                check=True, capture_output=True, text=True, timeout=1800,
            )
            launched = json.loads(result.stdout.splitlines()[-1])
            if (launched['state'] != 'worker-recorded' or launched['expected'] != op['expected']
                    or any(launched.get(key) is not value for key,value in AUTHORITY.items())):
                raise ValueError('object worker launch receipt differs')
        elif target == 'pair-recorded':
            matches = [row for row in classify_pending(PENDING, LEDGER) if row['expected'] == op['expected']]
            if len(matches) != 1:
                raise ValueError('object proof not found')
            if matches[0]['status'] == 'pair-reconciliation-needed':
                run_pair_once(op)
            elif matches[0]['status'] != 'provisional-pair-recorded':
                raise ValueError('object proof not verified')

    def observe(self, op):
        target, intent, expected = op['target'], op['intent'], op['expected']
        if target == 'gws-verified' and 'preparation' in intent:
            out = Path(intent['preparation']['out'])
            resolve_preparation(out, intent_preparation=intent['preparation'])
            verified = out/'captured-gws-evidence'/'verification.json'
            if not verified.is_file():
                identity_path = out/'service-identity.json'
                if not identity_path.is_file():
                    return None
                identity = read_json(identity_path)
                if identity['unit'] != intent['unit']:
                    raise ValueError('GWS worker differs from intent')
                properties = state(intent['unit'])
                if (properties.get('InvocationID') != identity['invocation_id']
                        or properties.get('ExecMainPID') != str(identity['main_pid'])):
                    raise RuntimeError('recorded GWS worker identity no longer matches')
                if properties.get('SubState') == 'failed' and properties.get('Result') == 'exit-code':
                    raise FailedGwsWorker('recorded GWS worker failed')
                if properties.get('ActiveState') in ('inactive','failed'):
                    raise RuntimeError('recorded GWS worker stopped without accepted evidence')
                if not successful(properties):
                    return None
                capture(out)
            register_captured(out, PENDING)
        if target == 'gws-verified':
            if expected not in verified_pending(PENDING):
                return None
            evidence = {'pending_database':PENDING.name}
        elif target == 'object-verified':
            if 'preparation' in intent:
                preparation = Path(intent['preparation'])
                landed = preparation/'landed-object-readback.json'
                if landed.exists() and read_json(landed).get('operation_id') != op['id']:
                    raise ValueError('landed object proof belongs to a different queue operation')
                completion = preparation/'gws-pending-object-complete.json'
                if (not completion.is_file() or os.path.lexists(OBJECT_GUARD)) and not recover_object(
                        intent['preparation'], expected):
                    return None
                if (BASE/'gws-pending-object-unresolved.json').exists():
                    return None
                value = read_json(completion)
                if value['expected'] != expected or any(value.get(k) is not v for k,v in AUTHORITY.items()):
                    raise ValueError('object completion differs')
            else:
                value = None
            matches = [row for row in classify_pending(PENDING, LEDGER) if row['expected'] == expected]
            if len(matches) != 1 or matches[0]['status'] == 'object-copy-needed':
                return None
            evidence = value or {'operational_database':LEDGER.name}
        else:
            evidence = self.pair_checkpoint(expected)
            if evidence is None:
                return None
        return {'operation_id':op['id'], 'expected':expected, 'stage':target,
                'verified':True, 'evidence':evidence, **AUTHORITY}

    @staticmethod
    def pair_checkpoint(expected):
        if (BASE/'gws-first-pair-unresolved.json').exists():
            return None
        matches = [row for row in classify_pending(PENDING, LEDGER) if row['expected'] == expected]
        if len(matches) != 1 or matches[0]['status'] != 'provisional-pair-recorded':
            return None
        for marker in sorted(BASE.glob('gws-first-pair-reconcile-*/resolved-marker.json'), reverse=True):
            intent = read_json(marker)
            if intent['relative_path'] != expected['relative_path']:
                continue
            gws = read_json(Path(intent['gws_evidence']))
            if gws['expected'] != expected:
                continue
            events = [json.loads(line) for line in (marker.parent/'journal.jsonl').read_bytes().splitlines()]
            proof = read_json(marker.parent/'independent-verification.json')
            if (events[-1]['phase'] != 'complete' or proof['sha256'] != events[-1]['sha256']
                    or proof.get('gws', {}).get('readback_verified') is not True
                    or proof.get('object', {}).get('readback_verified') is not True
                    or any(proof.get(k) is not v for k,v in AUTHORITY.items())):
                raise ValueError('pair checkpoint proof differs')
            return {'snapshot_sha256':proof['sha256'], 'proof':str(marker.parent/'independent-verification.json')}
        return None


def retire_capacity_admission(out, prep, error):
    unit = 'aurora-direct-'+prep['operation']+'.service'
    if (state(unit).get('LoadState') != 'not-found'
            or any(os.path.lexists(out/name) for name in
                   ('launch-intent.json','service-identity.json','captured-gws-evidence'))):
        raise ValueError('capacity-rejected admission has possible transfer evidence')
    receipt = {'admission_unit':prep['admission_unit'], 'transfer_unit':unit,
               'admission_identity':state(prep['admission_unit']), 'blocker':str(error), **AUTHORITY}
    save(out/'capacity-rejection.json', (json.dumps(receipt, sort_keys=True)+'\n').encode())
    destination = out.with_name('capacity-rejected-'+out.name)
    if os.path.lexists(destination):
        raise ValueError('capacity rejection quarantine already exists')
    out.rename(destination)
    fd = os.open(out.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare_one(candidate, *, long_gws=False):
    """Create one exact-version admission from a frozen metadata candidate."""
    relative = candidate['relative_path']
    gws_only_runner.probe_target(relative)
    prep = prepare(relative, dated_id=True, long_worker=long_gws)
    out = Path(prep['out'])
    expected = candidate_identity(out)
    if (expected['size'] != candidate['size'] or expected['mtime_ns']//10**9 != candidate['mtime']
            or expected['relative_path'] != relative):
        raise ValueError('candidate changed since inventory')
    try:
        admission(prep['admission_unit'], read_json(out/'request.json'))
    except AdmissionCapacityUnavailable as error:
        retire_capacity_admission(out, prep, error)
        raise
    return prep, expected


def load_candidates(path, expected_sha256):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('frozen candidate manifest hash differs')
    manifest = json.loads(raw)
    if any(manifest.get(k) is not v for k,v in AUTHORITY.items()):
        raise ValueError('inventory carries unexpected authority')
    rows = manifest['candidates']
    if len(rows) != manifest['candidate_count'] or len({row['relative_path'] for row in rows}) != len(rows):
        raise ValueError('frozen candidate count differs or contains duplicates')
    return rows


def next_candidate(rows):
    known = gws_only_runner.known_paths()
    return next((item for item in rows if item['relative_path'] not in known), None)


def _step(queue, candidates, *, reconcile_only, long_gws=False):
    pending = queue.outstanding()
    if pending:
        wait = queue.recovery_wait(pending['id'])
        if wait and time.time() < wait['retry_at']:
            return 'waiting-for-recovery'
        if wait and wait['blocker'] == 'pair-gws-transient':
            if pending['target'] != 'pair-recorded':
                raise ValueError('pair retry attached to wrong operation')
            receipt=Driver().observe(pending)
            if receipt is not None:
                queue.accept(pending,receipt)
                return 'advanced'
            retry_pair_under_lock(pending['id'])
            return advance(queue, Driver(), reconcile_only=True)
        # Reconciliation never starts another transfer.
        if pending['target'] == 'gws-verified':
            with writer_lock(BASE/'bounded-backlog-coordinator.lock'):
                if 'preparation' in pending['intent']:
                    out = Path(pending['intent']['preparation']['out'])
                    if not out.exists() and out.with_name('interrupted-'+out.name).is_dir():
                        reconcile_partial_gws(queue, pending)
                        return 'advanced'
                try:
                    return advance(queue, Driver(), reconcile_only=True)
                except FailedGwsWorker:
                    try:
                        reconcile_failed_gws(pending)
                    except GwsCapacityUnavailable as error:
                        queue.defer_recovery(pending['id'], str(error))
                        return 'waiting-for-recovery'
                    except IncompleteGwsStage:
                        reconcile_partial_gws(queue, pending)
                        return 'advanced'
                    return advance(queue, Driver(), reconcile_only=True)
        try:
            return advance(queue, Driver(), reconcile_only=True)
        except FailedObjectWorker:
            landed = False
            with writer_lock(BASE/'bounded-backlog-coordinator.lock'):
                prep = Path(pending['intent']['preparation'])
                try:
                    reconcile_failed_object(queue, pending['id'], prep)
                except ObjectStoreUnavailable as error:
                    queue.defer_recovery(pending['id'], str(error))
                    return 'waiting-for-recovery'
                except VerifiedLandedObject:
                    landed = True
            if landed:
                return advance(queue, Driver(), reconcile_only=True)
            return 'advanced'
    if os.path.lexists(OBJECT_GUARD):
        with writer_lock(BASE/'bounded-backlog-coordinator.lock'):
            prep = Path(read_json(OBJECT_GUARD)['preparation'])
            operation = queue.retired_object_operation(prep)
            reconcile_failed_object(queue, operation, prep)
        return 'advanced'
    if reconcile_only:
        return 'no-outstanding-operation'
    require_reconciled(BASE)
    check_retention_and_fence()
    import_pending(queue)
    queued = queue.next()
    if queued:
        expected, stage = queued
        if stage != 'discovered' or expected in verified_pending(PENDING):
            return advance(queue, Driver())
        candidate = {'relative_path':expected['relative_path'], 'size':expected['size'],
                     'mtime':expected['mtime_ns']//10**9}
        with writer_lock(BASE/'bounded-backlog-coordinator.lock'):
            prep, observed = prepare_one(candidate, long_gws=long_gws)
            if observed != expected:
                raise ValueError('queued GWS source version changed before retry')
            return advance(queue, Driver(prep))
    if candidates is None:
        return 'queue-exhausted'
    with writer_lock(BASE/'bounded-backlog-coordinator.lock'):
        candidate = next_candidate(candidates)
        if candidate is None:
            return 'queue-exhausted'
        prep, expected = prepare_one(candidate, long_gws=long_gws)
        queue.add(expected)
        return advance(queue, Driver(prep))


def step(queue, candidates, *, reconcile_only, long_gws=False):
    try:
        return _step(queue,candidates,reconcile_only=reconcile_only,long_gws=long_gws)
    except PairVerificationUnavailable:
        op=queue.outstanding()
        if not op or op['target']!='pair-recorded':raise
        queue.defer_recovery(op['id'],'pair-gws-transient')
        return 'waiting-for-recovery'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('reconcile', 'run', 'status'))
    parser.add_argument('--candidates', type=Path)
    parser.add_argument('--candidates-sha256')
    parser.add_argument('--long-gws',action='store_true')
    parser.add_argument('--largest-first',action='store_true')
    parser.add_argument('--watch-existing',action='store_true')
    parser.add_argument('--steps', type=int, default=1)
    parser.add_argument('--stop-after-checkpoints', type=int)
    args = parser.parse_args()
    if not 1 <= args.steps <= 5000:
        parser.error('--steps must be between 1 and 5000')
    if bool(args.candidates) != bool(args.candidates_sha256):
        parser.error('--candidates and --candidates-sha256 must be supplied together')
    candidates = load_candidates(args.candidates,args.candidates_sha256) if args.candidates else None
    if args.largest_first:
        if candidates is None:
            parser.error('--largest-first requires a frozen candidate manifest')
        candidates = sorted(candidates,key=lambda item:(-item['size'],item['relative_path']))
    if args.action == 'status':
        with contextlib.closing(Queue(STATE, readonly=True)) as queue:
            print(json.dumps(queue.status(), sort_keys=True))
        return
    with writer_lock(BASE/'per-file-coordinator.lock'), contextlib.closing(Queue(STATE)) as queue:
        watch_existing = args.action == 'run' and args.watch_existing
        for _ in range(args.steps if args.action == 'run' else 1):
            if (args.stop_after_checkpoints is not None
                    and queue.status()['counts']['checkpoint-replicated'] >= args.stop_after_checkpoints):
                break
            try:
                result = step(queue, candidates, reconcile_only=args.action == 'reconcile',
                              long_gws=args.long_gws)
            except AdmissionCapacityUnavailable as error:
                previous = read_json(STATUS) if STATUS.is_file() else {}
                prior = previous.get('recovery', {})
                blocker = str(error)
                attempts = prior.get('attempts', 0) + 1 if prior.get('blocker') == blocker else 1
                value = {'result':'waiting-for-capacity', **queue.status(),
                         'recovery':{'blocker':blocker, 'attempts':attempts,
                                     'retry_at':time.time()+300, 'persistent_alert':attempts >= 3}}
                publish_status(value)
                print(json.dumps(value, sort_keys=True), flush=True)
                raise SystemExit(75) from None
            value = {'result':result, **queue.status()}
            publish_status(value)
            print(json.dumps(value, sort_keys=True), flush=True)
            if result == 'awaiting-reconciliation' and watch_existing:
                deadline = time.monotonic()+6*3600+1200
                while result == 'awaiting-reconciliation' and time.monotonic() < deadline:
                    time.sleep(5)
                    result = step(queue, candidates, reconcile_only=True, long_gws=args.long_gws)
                if result != 'awaiting-reconciliation':
                    value = {'result':result, **queue.status()}
                    publish_status(value)
                    print(json.dumps(value, sort_keys=True), flush=True)
                else:
                    raise RuntimeError('recorded worker still requires reconciliation after wait window')
            if result == 'waiting-for-recovery':
                operation = queue.outstanding()
                value = {'result':result, **queue.status(),
                         'recovery':queue.recovery_wait(operation['id'])}
                publish_status(value)
                print(json.dumps(value, sort_keys=True), flush=True)
                if args.action == 'run':
                    raise SystemExit(75)
            if result == 'awaiting-reconciliation' and args.action == 'run':
                raise SystemExit(75)
            if result != 'advanced':
                break


if __name__ == '__main__':
    main()
