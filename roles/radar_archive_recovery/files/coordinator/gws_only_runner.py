#!/usr/bin/env python3
"""Bounded GWS-only backlog worker; no object, eviction or retention claim."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import sqlite3

import gws_only_sequence
from bounded_backlog_runner import admission, launch, wait_unit
from capture_gws_only import capture
from gws_pending_ledger import register_captured
from prepare_direct_candidate import BASE, run_remote
from prepare_gws_only_candidate import prepare

HERE = Path(__file__).resolve().parent


def probe_target(path):
    code = (HERE / 'gws_new_target_probe.py').read_text()
    ssh = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
           '-o', 'StrictHostKeyChecking=yes', '-o', 'IdentitiesOnly=yes',
           '-i', '/home/aurora/.ssh/id_rsa_jasmin_20200514', 'rrniii@xfer-vm-03.jasmin.ac.uk',
           shlex.join(['python3', '-c', code, path])]
    result = json.loads(run_remote(['sudo', '-n', '-u', 'aurora', '--', *ssh], timeout=60).stdout)
    if not (result.get('relative_path') == path and result.get('canonical_present') is False
            and result.get('partials') == [] and result.get('eviction_authorized') is False
            and result.get('retention_authorized') is False):
        raise ValueError('GWS target or prior partial requires reconciliation')
    return {'accepted': True, 'directory_identity': result['directory_identity']}


def exclusive_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, sort_keys=True); f.write('\n'); f.flush(); os.fsync(f.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(directory)
    finally: os.close(directory)


class Driver:
    def probe(self, path): return probe_target(path)

    def prepare(self, path):
        self.prepared = prepare(path)
        self.out = Path(self.prepared['out'])
        self.request = json.loads((self.out / 'request.json').read_bytes())
        if self.request.get('relative_path') != path: raise ValueError('prepared path differs')
        return {'accepted': True, 'output': str(self.out), 'unit': self.prepared['admission_unit']}

    def admit(self, path):
        if self.request['relative_path'] != path: raise ValueError('admission path differs')
        return admission(self.prepared['admission_unit'], self.request)

    def transfer(self, path, *, launch_only=False):
        if self.request['relative_path'] != path: raise ValueError('transfer path differs')
        probe_target(path)
        unit = 'aurora-direct-' + self.prepared['operation'] + '.service'
        intent = {'unit': unit, 'relative_path': path, 'retention_authorized': False,
                  'eviction_authorized': False, 'scope': 'GWS only; object not verified'}
        exclusive_json(self.out / 'launch-intent.json', intent)
        identity = launch(unit, self.out)
        exclusive_json(self.out / 'service-identity.json', identity)
        if launch_only:
            return {'accepted': True, **identity}
        props = wait_unit(unit, 7500)
        if props.get('InvocationID') != identity['invocation_id'] or props.get('ExecMainPID') != str(identity['main_pid']):
            raise ValueError('worker identity changed')
        return {'accepted': True, **identity}

    def capture(self, path):
        result = capture(self.out)
        if result.get('expected', {}).get('relative_path') != path or result.get('object_verified') is not False:
            raise ValueError('captured GWS evidence differs')
        pending = register_captured(self.out, BASE / 'gws-only-pending.sqlite')
        return {'accepted': True, 'verification': str(self.out / 'captured-gws-evidence' / 'verification.json'),
                'pending_evidence_sha256': pending['evidence_sha256']}


def known_paths(*, isolated_candidate=None, object_operation=None, lock_fds=None):
    known = set()
    if os.path.lexists(BASE/'gws-first-pair-unresolved.json'):
        raise ValueError('unresolved pairing or object operation requires reconciliation')
    guard = BASE/'gws-pending-object-unresolved.json'
    proof = None
    if os.path.lexists(guard):
        if isolated_candidate is None:
            raise ValueError('unresolved pairing or object operation requires reconciliation')
        from gws_object_isolation import check
        proof = check(isolated_candidate, object_operation, lock_fds=lock_fds, base=BASE)
        if proof.get('object_isolated') is not True:
            raise ValueError('object operation is not isolated')
    for prep in BASE.glob('direct-gws-only-preparation-*'):
        proof = prep / 'captured-gws-evidence' / 'verification.json'
        if not proof.is_file(): raise ValueError('prior GWS-only preparation requires reconciliation')
        value = json.loads(proof.read_bytes())
        if not (value.get('state') == 'gws-present-verified' and value.get('object_verified') is False
                and value.get('eviction_authorized') is False and value.get('retention_authorized') is False):
            raise ValueError('prior GWS-only evidence invalid')
        known.add(value['expected']['relative_path'])
    marker = BASE / 'bounded-backlog-unresolved.json'
    if os.path.lexists(marker): known.add(json.loads(marker.read_bytes())['relative_path'])
    db = sqlite3.connect('file:' + str(BASE / 'working-ledger.sqlite') + '?mode=ro', uri=True)
    try:
        db.execute('BEGIN')
        for table in ('provisional_object_versions', 'provisional_gws_versions'):
            known.update(row[0] for row in db.execute('SELECT DISTINCT relative_path FROM ' + table))
    finally: db.close()
    if proof is not None and check(isolated_candidate, object_operation, lock_fds=lock_fds, base=BASE) != proof:
        raise ValueError('object isolation changed while enumerating known paths')
    return known


def sync_captured_preparations():
    """A new batch may start only after all earlier captures enter the pending queue."""
    count = 0
    for prep in sorted(BASE.glob('direct-gws-only-preparation-*')):
        from delegated_gws_attempt import resolved_version
        if resolved_version(prep, BASE/'fresh-finalized-queue.sqlite') is not None:
            continue
        if not (prep/'captured-gws-evidence'/'verification.json').is_file():
            raise ValueError('prior GWS-only preparation requires reconciliation')
        register_captured(prep, BASE/'gws-only-pending.sqlite')
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--queue', type=Path, required=True)
    parser.add_argument('--count', type=int, required=True); parser.add_argument('--journal', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.count <= 20: raise ValueError('invalid batch size')
    lock = os.open(BASE / 'bounded-backlog-coordinator.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        known = known_paths()
        sync_captured_preparations()
        queue = json.loads(args.queue.read_bytes())
        paths = [row['relative_path'] for row in queue['candidates'] if row['relative_path'] not in known][:args.count]
        if len(paths) != args.count: raise ValueError('not enough distinct candidates')
        print(json.dumps({'completed': gws_only_sequence.run(paths, Driver(), args.journal),
                          'object_verified': False, 'eviction_authorized': False, 'retention_authorized': False}))
    finally: os.close(lock)


if __name__ == '__main__': main()
