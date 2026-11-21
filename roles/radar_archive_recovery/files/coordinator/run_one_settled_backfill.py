"""Run or reconcile one settled native backfill without duplicating attempts."""
import argparse
import json
import os
import stat
import time

from settled_backfill_controller import FRESH, reconcile, start
from per_file_queue import Queue


def run(manifest, digest, *, clock=time.monotonic, sleep=time.sleep, timeout=14400):
    deadline = clock() + timeout
    started = None
    while clock() < deadline:
        try:
            # A durable existing operation is always reconciled before any new
            # candidate can be reserved, including after a process restart.
            if os.path.lexists(FRESH):
                info = FRESH.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                    raise ValueError('fresh queue is not protected')
                queue = Queue(FRESH, readonly=True)
                try:
                    outstanding = queue.outstanding()
                    finished = queue.finished_receipt(started['id']) if started and outstanding is None else None
                finally: queue.close()
            else:
                outstanding = None
                finished = None
            if outstanding is not None:
                result = reconcile()
            elif started:
                if (not isinstance(finished, dict) or finished.get('operation_id') != started['id']
                        or finished.get('expected') != started['expected'] or finished.get('stage') != 'gws-verified'
                        or finished.get('verified') is not True):
                    raise ValueError('recorded operation disappeared without a verified receipt')
                return {'state': 'gws-verified', 'operation': started,
                        'retention_authorized': False, 'eviction_authorized': False}
            else:
                result = start(manifest, digest)
            print(json.dumps(result, sort_keys=True), flush=True)
            state = result['state']
            if state == 'started': started = result['operation']
            elif state == 'gws-verified': return result
            elif state == 'no-admissible-candidate': return result
            elif state not in {'awaiting-reconciliation', 'no-outstanding-operation'}:
                raise ValueError('unexpected backfill controller state')
        except BlockingIOError:
            pass
        except ValueError as error:
            if str(error) != 'an archive operation requires reconciliation':
                raise
        sleep(5)
    raise TimeoutError('backfill remains unresolved; reconcile its recorded operation')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.manifest, args.sha256), sort_keys=True), flush=True)


if __name__ == '__main__': main()
