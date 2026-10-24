#!/usr/bin/env python3
"""Admit bounded radar transfers and retire only verified duplicate partials."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time

GIB = 1024 ** 3


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def check_capacity(root, required_mount, workers, max_file_bytes, reserve_bytes):
    if workers < 1 or max_file_bytes < 0 or reserve_bytes < 0:
        raise ValueError('Invalid radar capacity reservation')
    root, required_mount = Path(root), Path(required_mount)
    if not required_mount.is_dir() or not os.path.ismount(required_mount):
        raise RuntimeError('Required radar data mount is missing: ' + str(required_mount))
    if not root.is_dir() or root.is_symlink() or root.stat().st_dev != required_mount.stat().st_dev:
        raise RuntimeError('Radar destination is missing or on the wrong filesystem')
    partial_root = root / '.radar-partials'
    if partial_root.is_symlink() or (partial_root.exists() and partial_root.stat().st_dev != root.stat().st_dev):
        raise RuntimeError('Radar partials must use the same mounted filesystem')
    available = os.statvfs(root)
    free_bytes = available.f_bavail * available.f_frsize
    required_bytes = reserve_bytes + workers * max_file_bytes
    # Closed days may live on /data behind read-only binds. Check those mounts
    # as well as /project whenever the separately commissioned layout is active.
    relocation = Path('/etc/aurora/radar-relocation-active.json')
    if relocation.exists() or relocation.is_symlink():
        subprocess.run(['/usr/local/lib/aurora-radar-relocation.py', 'guard',
                        '--config', str(relocation), '--additional-bytes',
                        str(workers * max_file_bytes)], check=True)
    if free_bytes < required_bytes:
        raise RuntimeError(f'Radar capacity hold: available={free_bytes}, required={required_bytes}, workers={workers}, largest_file={max_file_bytes}')
    return {'available_bytes': free_bytes, 'required_bytes': required_bytes}


def retire_duplicates(root, relative, workers, audit, minimum_age_seconds=21600):
    """Bound cleanup to one completed file and at most 16 attempts per worker."""
    root, relative, audit = Path(root).resolve(strict=True), Path(relative), Path(audit)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('Expected a contained radar relative path')
    target = root / relative
    partial_root = root / '.radar-partials'
    if partial_root.is_symlink() or target.is_symlink() or not target.is_file():
        return 0
    if not target.resolve().is_relative_to(root):
        return 0
    reclaimed = 0
    candidates = []
    for worker in range(workers):
        legacy = partial_root / str(worker) / relative
        candidates.append(legacy)
        candidates.extend(sorted(legacy.parent.glob(legacy.name + '.incoming.*'), key=lambda p: p.lstat().st_mtime)[:16])
    for partial in candidates:
        if partial.is_symlink() or not partial.is_file():
            continue
        if not partial.resolve().is_relative_to(partial_root):
            continue
        before, target_before = partial.lstat(), target.lstat()
        if (not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(target_before.st_mode)
                or not before.st_size or target_before.st_size < before.st_size
                or time.time() - before.st_mtime < minimum_age_seconds):
            continue
        digest = hashlib.sha256()
        matches = True
        with partial.open('rb') as left, target.open('rb') as right:
            while chunk := left.read(4 * 1024 * 1024):
                if chunk != right.read(len(chunk)):
                    matches = False
                    break
                digest.update(chunk)
            if matches:
                # Never remove the spare bytes before the complete target is durable.
                os.fsync(right.fileno())
        if not matches or signature(before) != signature(partial.lstat()) or signature(target_before) != signature(target.lstat()):
            continue
        record = {'time': time.time(), 'partial': str(partial), 'target': str(target),
                  'bytes': before.st_size, 'prefix_sha256': digest.hexdigest(), 'action': 'verified'}
        audit.parent.mkdir(parents=True, exist_ok=True)
        with audit.open('a') as log:
            log.write(json.dumps(record) + '\n')
            log.flush()
            os.fsync(log.fileno())
            if signature(before) != signature(partial.lstat()) or signature(target_before) != signature(target.lstat()):
                continue
            partial.unlink()
            directory = os.open(partial.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            log.write(json.dumps({**record, 'action': 'removed_partial'}) + '\n')
            log.flush()
            os.fsync(log.fileno())
        reclaimed += before.st_size
    return reclaimed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    capacity = sub.add_parser('capacity')
    capacity.add_argument('--root', type=Path, required=True)
    capacity.add_argument('--required-mount', type=Path, required=True)
    capacity.add_argument('--workers', type=int, required=True)
    capacity.add_argument('--max-file-bytes', type=int, required=True)
    capacity.add_argument('--reserve-bytes', type=int, default=8 * GIB)
    cleanup = sub.add_parser('retire-duplicates')
    cleanup.add_argument('--root', type=Path, required=True)
    cleanup.add_argument('--relative', required=True)
    cleanup.add_argument('--workers', type=int, required=True)
    cleanup.add_argument('--audit', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == 'capacity':
            print(json.dumps(check_capacity(args.root, args.required_mount, args.workers, args.max_file_bytes, args.reserve_bytes)))
        else:
            print(json.dumps({'reclaimed_bytes': retire_duplicates(args.root, args.relative, args.workers, args.audit)}))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 75


if __name__ == '__main__':
    raise SystemExit(main())
