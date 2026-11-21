#!/usr/bin/env python3
"""Serialize raw GWS batches; let waiting fast delivery run between bulk batches."""
import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time


class DeliveryBusy(RuntimeError):
    pass


def open_lock(path):
    return os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660)


@contextmanager
def delivery_lock(root, priority, wait_seconds=0):
    """Fast waiters keep a shared intent lock until their delivery is complete.

    Bulk holds the exclusive intent lock only while trying the delivery lock.
    It never queues ahead of a waiting dispatcher. Kernel locks disappear after
    a crash; a stale marker cannot permanently block either lane.
    """
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError('Expected the configured delivery lock directory')
    intent = open_lock(root / 'fast-waiting.lock')
    delivery = None
    deadline = time.monotonic() + wait_seconds
    try:
        delivery = open_lock(root / 'delivery.lock')
        while True:
            try:
                fcntl.flock(intent, (fcntl.LOCK_SH if priority == 'fast' else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if priority == 'bulk' or (wait_seconds and time.monotonic() >= deadline):
                    raise DeliveryBusy('GWS delivery deferred: fast lane is waiting')
                time.sleep(0.05)
        while True:
            try:
                fcntl.flock(delivery, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                # A bulk waiter must not hold the intent gate ahead of fast work.
                if priority == 'bulk':
                    fcntl.flock(intent, fcntl.LOCK_UN)
                    raise DeliveryBusy('GWS delivery deferred: another batch is active')
                if wait_seconds and time.monotonic() >= deadline:
                    raise DeliveryBusy('GWS delivery deferred: active batch has not finished')
                time.sleep(0.05)
        if priority == 'bulk':
            fcntl.flock(intent, fcntl.LOCK_UN)
        yield delivery
    finally:
        if delivery is not None:
            os.close(delivery)
        os.close(intent)


def supervised_run(command, lock_fd):
    """Keep ownership until rsync and its foreground SSH child have returned.

    Ordinary TERM/INT/HUP requests are remembered, not forwarded into a remote
    copy. Return their status after the current batch drains. The local child
    also inherits the descriptor; forced termination still needs a receiver check.
    """
    requested = []
    previous = {}
    def remember(signum, _frame):
        requested.append(signum)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous[signum] = signal.signal(signum, remember)
    process = None
    try:
        process = subprocess.Popen(command, pass_fds=(lock_fd,))
        result = process.wait()
        return 128 + requested[0] if requested else result
    finally:
        # Even an unexpected local exception cannot release ownership mid-copy.
        if process is not None:
            process.wait()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run_batch(command, lock_root, priority, wait_seconds=0):
    with delivery_lock(lock_root, priority, wait_seconds) as lock_fd:
        return supervised_run(command, lock_fd)


def discover(command, source, destination):
    """Rsync performs its existing destination quick check outside the lock.

    Only names are reused. The actual batch builds a fresh rsync file list while
    holding the delivery lock, so discovery cannot publish an old source version.
    Unrecognised records fail closed rather than silently omitting raw paths.
    """
    args = [x for x in command if x not in ('--stats', '--human-readable')]
    args += ['--dry-run', '--out-format=AURORA\t%i\t%l\t%n\t%L', source, destination]
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode not in (0, 24):
        sys.stderr.write(result.stderr)
        raise RuntimeError('GWS discovery failed with rsync exit ' + str(result.returncode))
    if result.returncode == 24:
        sys.stderr.write(result.stderr)
    paths = {}
    def checked_name(name):
        if '\\#' in name or '\x00' in name:
            raise RuntimeError('Unsafe or encoded raw archive filename: ' + name[:160])
        relative = Path(name)
        if not name or relative.is_absolute() or '..' in relative.parts:
            raise RuntimeError('Raw archive path escapes its source')
        return name

    for line in result.stdout.splitlines():
        fields = line.split('\t', 4)
        if len(fields) != 5 or fields[0] != 'AURORA':
            raise RuntimeError('Unrecognised rsync discovery output: ' + line[:160])
        change, size, name, link = fields[1:]
        if len(change) != 11 or change[1] not in 'fdL':
            raise RuntimeError('Unsupported raw archive entry: ' + line[:160])
        if not size.isdecimal():
            raise RuntimeError('Invalid raw archive size: ' + size[:160])
        paths[checked_name(name)] = int(size)
        if change[0] == 'h':
            # Rsync can omit an unchanged peer from its output. Include that
            # peer so --files-from retains the original -H relationship.
            if not link.startswith(' => '):
                raise RuntimeError('Missing rsync hardlink reference')
            paths[checked_name(link[4:])] = int(size)
    return list(paths.items()), result.returncode


def batches(paths, source, max_files, max_bytes, copy_links=False):
    """Keep discovered hardlink peers together, even if a group exceeds a bound."""
    groups = {}
    for index, (name, size) in enumerate(paths):
        key = ('path', index)
        try:
            path = Path(source) / name
            info = path.stat() if copy_links else path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                key = ('inode', info.st_dev, info.st_ino)
        except FileNotFoundError:
            pass  # Actual rsync retains the existing vanished-source semantics.
        groups.setdefault(key, []).append((name, size))
    # Build the hardlink index outside the delivery lock. Only the small next
    # batch is re-statted while locked; a long discovery cannot freeze its sizes.
    def refreshed(group):
        result = []
        for name, size in group:
            try:
                path = Path(source) / name
                info = path.stat() if copy_links else path.lstat()
                size = info.st_size
            except FileNotFoundError:
                pass
            result.append((name, size))
        return result

    def selected():
        current, total = [], 0
        for group in groups.values():
            fresh = refreshed(group)
            size = sum(item[1] for item in fresh)
            if current and (len(current) + len(fresh) > max_files or total + size > max_bytes):
                yield current
                current, total = [], 0
                fresh = refreshed(group)
                size = sum(item[1] for item in fresh)
            current.extend(fresh)
            total += size
        if current:
            yield current
    return selected()


def bulk(args):
    command = ['rsync', '-aH', '--partial', '--delay-updates', '--stats', '--human-readable', '-e', args.ssh_command]
    if args.copy_links:
        command.append('-L')
    for pattern in args.exclude:
        command.append('--exclude=' + pattern)
    source = args.source.rstrip('/') + '/'
    destination = args.destination.rstrip('/') + '/'
    paths, warning = discover(command, source, destination)
    pending = batches(paths, source, args.max_files, args.max_bytes, args.copy_links)
    number = 0
    while True:
        with delivery_lock(args.lock_root, 'bulk', args.wait_seconds) as lock_fd:
            batch = next(pending, None)
            if batch is None:
                return warning
            number += 1
            size = sum(item[1] for item in batch)
            print(f'GWS raw batch {number}: {len(batch)} paths, {size} selected bytes' +
                  (' (one oversized file/hardlink group)' if len(batch) > args.max_files or size > args.max_bytes else ''), flush=True)
            with tempfile.TemporaryDirectory(prefix='aurora-gws-batch-') as temporary:
                names = Path(temporary) / 'files.nul'
                names.write_bytes(b''.join(os.fsencode(name) + b'\0' for name, _ in batch))
                # --files-from intentionally prevents directory entries recursively
                # expanding beyond this batch. -aH still preserves their metadata.
                actual = command + ['--from0', '--files-from=' + str(names), source, destination]
                result = supervised_run(actual, lock_fd)
        if result not in (0, 24):
            return result  # In particular, exit 23 is never success.
        if result == 24:
            warning = 24  # Preserve the wrapper's existing vanished-source notice.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    for mode in ('bulk', 'fast'):
        child = sub.add_parser(mode)
        child.add_argument('--lock-root', required=True)
        child.add_argument('--wait-seconds', type=float, default=0,
                           help='Fast wait deadline; zero retains priority until the active batch drains. Bulk never waits.')
        if mode == 'bulk':
            child.add_argument('--source', required=True)
            child.add_argument('--destination', required=True)
            child.add_argument('--ssh-command', required=True)
            child.add_argument('--exclude', action='append', default=[])
            child.add_argument('--copy-links', action='store_true')
            child.add_argument('--max-files', type=int, default=256)
            child.add_argument('--max-bytes', type=int, default=64 * 1024**2)
        else:
            child.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.wait_seconds < 0 or (args.mode == 'bulk' and (args.max_files < 1 or args.max_bytes < 1)):
        parser.error('Expected positive batch limits and a nonnegative lock wait')
    try:
        if args.mode == 'bulk':
            return bulk(args)
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        if not command:
            parser.error('Missing rsync command')
        return run_batch(command, args.lock_root, 'fast', args.wait_seconds)
    except DeliveryBusy as error:
        print(str(error), file=sys.stderr)
        return 75  # Deferred; preserve queue state and retry on the normal timer.
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
