#!/usr/bin/env python3
"""Guard only canonical Power mirror writes and the scoped planning-cache copy."""
from pathlib import Path
import argparse
import json
import os
import signal
import subprocess
import sys
import time

GIB = 1024**3
SOURCE = Path('/data/aurora/products/power/ecmwf_solar_planning')
TARGET = Path('/srv/aurora-relocated/canonical-power/ecmwf_solar_planning')
DATA = Path('/data')
ROOT = Path('/')
ROOT_STOP = 22 * GIB
DATA_STOP = 10 * GIB


def free(path):
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def validate_layout(mode):
    if not os.path.ismount(DATA) or DATA.stat().st_dev == ROOT.stat().st_dev:
        raise RuntimeError('/data is not the expected separate mounted device')
    if SOURCE.is_symlink() or TARGET.is_symlink():
        raise RuntimeError('cache endpoints must not be symlinks')
    if not SOURCE.is_dir() or not TARGET.is_dir():
        raise RuntimeError('cache endpoints must exist as directories')
    if TARGET.stat().st_dev != ROOT.stat().st_dev:
        raise RuntimeError('planning cache destination is not root-backed')
    if mode == 'mounted':
        if not os.path.ismount(SOURCE) or not os.path.samefile(SOURCE, TARGET):
            raise RuntimeError('canonical planning bind is missing or points elsewhere')
    elif os.path.ismount(SOURCE) or SOURCE.stat().st_dev != DATA.stat().st_dev:
        raise RuntimeError('copy source is not the original data-backed directory')


def check(mode='mounted'):
    validate_layout(mode)
    available = {'rootBytes': free(ROOT), 'dataBytes': free(DATA)}
    if available['rootBytes'] < ROOT_STOP or available['dataBytes'] < DATA_STOP:
        raise RuntimeError('Power mirror reserve hold: ' + json.dumps(available))
    return available


def stop(child):
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)


def run(command, mode='mounted'):
    check(mode)
    if mode == 'copy':
        entries = list(SOURCE.rglob('*'))
        if any(p.is_symlink() for p in entries):
            raise RuntimeError('copy inventory contains symlinks')
        logical_bytes = sum(p.stat().st_size for p in entries if p.is_file())
        if free(ROOT) < logical_bytes + 24 * GIB:
            raise RuntimeError('copy requires its full logical size plus 24 GiB on root')
    child = subprocess.Popen(command, start_new_session=True)
    started = time.monotonic()
    old_handlers = {}
    def interrupted(signum, _frame):
        stop(child)
        raise RuntimeError(f'transfer interrupted by signal {signum}')
    for signum in (signal.SIGTERM, signal.SIGINT):
        old_handlers[signum] = signal.signal(signum, interrupted)
    try:
        while True:
            try:
                result = child.wait(timeout=2)
                if result in {0, 24}:
                    check(mode)  # no success stamp if the completed write crossed a reserve
                return result if result >= 0 else 128 - result
            except subprocess.TimeoutExpired:
                check(mode)
                if time.monotonic() - started > (1800 if mode == 'copy' else 3600):
                    raise RuntimeError('bounded Power transfer deadline exceeded')
    finally:
        stop(child)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['mounted', 'copy'], default='mounted')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.check:
            print(json.dumps(check(args.mode)))
            return 0
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        if not command:
            raise RuntimeError('no transfer command supplied')
        return run(command, args.mode)
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 75


if __name__ == '__main__':
    raise SystemExit(main())
