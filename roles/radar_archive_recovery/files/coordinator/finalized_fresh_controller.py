#!/usr/bin/env python3
"""Reserve, start or reconcile one finalized GWS operation under archive locks."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from finalized_admission import reserve
from finalized_queue_driver import FinalizedGwsDriver
from per_file_coordinator import BASE, STATE, candidate_identity
from per_file_queue import Queue

FRESH = BASE / 'fresh-finalized-queue.sqlite'
R7 = '/var/lib/aurora-radar-coordinator-v1/finalized-coordinator-r12-20261114/coordinator-r12/per_file_coordinator.py'


def require_r7():
    raw = subprocess.run(['systemctl', 'show', 'aurora-radar-coordinator.service', '-p', 'ActiveState',
                          '-p', 'SubState', '-p', 'MainPID', '-p', 'ExecStart'], check=True,
                         capture_output=True, text=True, timeout=20).stdout
    props = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    if (props.get('ActiveState') != 'active' or props.get('SubState') != 'running'
            or not props.get('MainPID', '').isdigit() or int(props['MainPID']) <= 0
            or 'argv[]=/usr/bin/python3 ' + R7 + ' run ' not in props.get('ExecStart', '')):
        raise ValueError('compatible historical coordinator is not running')
    command = (Path('/proc') / props['MainPID'] / 'cmdline').read_bytes().split(b'\0')
    if len(command) < 3 or command[:3] != [b'/usr/bin/python3', R7.encode(), b'run']:
        raise ValueError('running coordinator process does not match the compatible release')


@contextlib.contextmanager
def archive_locks(base=BASE):
    descriptors = {}
    try:
        for name in ('per-file-coordinator.lock', 'bounded-backlog-coordinator.lock'):
            path = base / name
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            descriptors[name] = fd
            info = os.fstat(fd)
            current = os.stat(path, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or info.st_nlink != 1 or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                raise ValueError('archive lock is not protected')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield descriptors
    finally:
        for fd in reversed(list(descriptors.values())): os.close(fd)


def start(preparation):
    require_r7()
    out = Path(preparation)
    if out.parent != BASE or not re.fullmatch(r'direct-finalized-preparation-20\d{6}T\d{6}Z', out.name):
        raise ValueError('invalid finalized preparation')
    request = json.loads((out / 'request.json').read_bytes())
    expected = candidate_identity(out)
    driver = FinalizedGwsDriver({'out': str(out), 'operation': request['operation_id']})
    with archive_locks() as fds, contextlib.closing(Queue(FRESH)) as fresh, contextlib.closing(Queue(STATE, readonly=True)) as frozen:
        require_r7()
        operation = reserve(fresh, frozen, expected, driver, lock_fds=fds)
        driver.apply(operation)
        return {'state': 'started', 'operation': operation, 'retention_authorized': False, 'eviction_authorized': False}


def reconcile():
    require_r7()
    with archive_locks(), contextlib.closing(Queue(FRESH)) as fresh:
        operation = fresh.outstanding()
        if operation is None:
            return {'state': 'no-outstanding-operation', 'retention_authorized': False, 'eviction_authorized': False}
        receipt = FinalizedGwsDriver().observe(operation)
        if receipt is None:
            return {'state': 'awaiting-reconciliation', 'operation': operation,
                    'retention_authorized': False, 'eviction_authorized': False}
        fresh.accept(operation, receipt)
        return {'state': 'gws-verified', 'operation': operation, 'retention_authorized': False, 'eviction_authorized': False}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'reconcile'))
    parser.add_argument('--preparation')
    args = parser.parse_args()
    if args.action == 'start' and not args.preparation: parser.error('start requires --preparation')
    print(json.dumps(start(args.preparation) if args.action == 'start' else reconcile(), sort_keys=True))


if __name__ == '__main__': main()
