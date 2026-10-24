#!/usr/bin/env python3
"""Preserve the completed 24 October recovery bundle on the system volume.

This is a one-off operator command. It never reads or changes the raw archive.
Run without --apply to inspect. An interrupted run is deliberately not resumed:
keep every copy and use the receipt to inspect the last completed step.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

SOURCE = Path('/data/aurora/recovery/radar-partials-20261024')
TARGET = Path('/var/lib/aurora-recovery/radar-partials-20261024')
RECEIPT = TARGET.with_name(TARGET.name + '.relocation.json')
EXPECTED_FILES = 104
EXPECTED_BYTES = 1_707_812_212


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def identity(path):
    s = path.lstat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns,
            s.st_mode, s.st_uid, s.st_gid, s.st_nlink]


def metadata(path):
    s = path.lstat()
    return [s.st_mode, s.st_uid, s.st_gid, s.st_mtime_ns,
            {key: os.getxattr(path, key).hex() for key in os.listxattr(path)}]


def digest(path):
    before = identity(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        sha = hashlib.sha256()
        while chunk := stream.read(4 * 1024 * 1024):
            sha.update(chunk)
        h = sha.hexdigest()
    require(before == identity(path), f'File changed during hashing: {path}')
    return h


def inventory(root, *, hashes=False):
    require(root.is_dir() and not root.is_symlink(), f'Expected real directory: {root}')
    device = root.stat().st_dev
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        s = path.lstat()
        require(s.st_dev == device and not os.path.ismount(path), f'Unexpected mount: {path}')
        require(stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode), f'Unexpected file type: {path}')
        if stat.S_ISREG(s.st_mode):
            require(s.st_nlink == 1, f'Hard-linked file: {path}')
        entry = {'identity': identity(path), 'metadata': metadata(path),
                 'directory': stat.S_ISDIR(s.st_mode), 'allocated': s.st_blocks * 512}
        if hashes and not entry['directory']:
            entry['sha256'] = digest(path)
        result[str(path.relative_to(root))] = entry
    return result


def stable_files(root, baseline):
    current = inventory(root)
    require(current.keys() == baseline.keys(), 'Recovery file set changed')
    for name, expected in baseline.items():
        require(current[name]['identity'] == expected['identity'], f'Recovery identity changed: {name}')


def verify_copy(root, baseline):
    current = inventory(root, hashes=True)
    require(current.keys() == baseline.keys(), 'Copied recovery file set differs')
    for name, expected in baseline.items():
        actual = current[name]
        require(actual['metadata'] == expected['metadata'], f'Copied metadata differs: {name}')
        if not expected['directory']:
            require(actual['identity'][2] == expected['identity'][2]
                    and actual['sha256'] == expected['sha256'], f'Copied bytes differ: {name}')
    return current


def no_open_users(*roots, proc_root=Path('/proc')):
    """Fail closed for descriptors or mappings in this small recovery tree."""
    prefixes = [str(root) for root in roots]
    def refers_to_tree(value):
        return any(value == root or value.startswith(root + '/') for root in prefixes)
    for proc in proc_root.glob('[0-9]*'):
        try:
            for handle in ('cwd', 'root'):
                try:
                    require(not refers_to_tree(os.readlink(proc / handle)),
                            f'Recovery contains the {handle} of PID {proc.name}')
                except FileNotFoundError:
                    pass
            for fd in (proc / 'fd').iterdir():
                try:
                    require(not refers_to_tree(os.readlink(fd)), f'Recovery has an open descriptor in PID {proc.name}')
                except FileNotFoundError:
                    pass
            for line in (proc / 'maps').read_text().splitlines():
                parts = line.split(maxsplit=5)
                require(len(parts) < 6 or not refers_to_tree(parts[5]),
                        f'Recovery has a mapping in PID {proc.name}')
        except (FileNotFoundError, ProcessLookupError):
            pass


def write_receipt(path, record):
    temp = path.with_name(path.name + '.writing')
    with temp.open('x') as out:
        json.dump(record, out, indent=2, sort_keys=True)
        out.write('\n')
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)
    sync_dir(path.parent)


def relocate(source, target, receipt, baseline, check_users=no_open_users):
    """Keep the original until the saved logical path passes a full readback."""
    staging = target.with_name(target.name + '.copying')
    backup = source.with_name(source.name + '.relocated-original')
    for path in (target, staging, backup, receipt, receipt.with_name(receipt.name + '.writing')):
        require(not os.path.lexists(path), f'Refusing existing path: {path}')
    check_users(source)
    stable_files(source, baseline)
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    require(not target.parent.is_symlink(), 'Target parent is a symlink')
    sync_dir(target.parent.parent)
    record = {'source': str(source), 'target': str(target), 'backup': str(backup),
              'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'files': baseline, 'removed_duplicates': [], 'phase': 'copying'}
    write_receipt(receipt, record)
    shutil.copytree(source, staging, copy_function=shutil.copy2)
    # copy2 preserves extended attributes and timestamps; ownership needs root.
    for name, entry in baseline.items():
        path = staging / name
        s = (source / name).stat()
        if (path.stat().st_uid, path.stat().st_gid) != (s.st_uid, s.st_gid):
            os.chown(path, s.st_uid, s.st_gid)
            shutil.copystat(source / name, path)
        if not entry['directory']:
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
    for name, entry in sorted(baseline.items(), key=lambda item: len(Path(item[0]).parts), reverse=True):
        if entry['directory']:
            sync_dir(staging / name)
    verify_copy(staging, baseline)
    stable_files(source, baseline)
    check_users(source, staging)
    staging.rename(target)
    sync_dir(target.parent)
    record['phase'] = 'copy_verified'
    write_receipt(receipt, record)
    source.rename(backup)
    sync_dir(source.parent)
    source.symlink_to(target, target_is_directory=True)
    sync_dir(source.parent)
    require(source.is_symlink() and source.resolve() == target, 'Logical recovery path does not resolve to target')
    # Check through the logical path, then prove the original compressed bytes
    # remain the same before any duplicate is released.
    verify_copy(source.resolve(), baseline)
    for name, entry in baseline.items():
        if not entry['directory']:
            require(identity(backup / name) == entry['identity'], f'Original changed: {name}')
            require(digest(source / name) == entry['sha256'], f'Logical readback failed: {name}')
    check_users(source, target, backup)
    record['phase'] = 'logical_path_verified'
    write_receipt(receipt, record)
    for name, entry in baseline.items():
        if entry['directory']:
            continue
        require(source.is_symlink() and source.resolve() == target, 'Logical path changed before duplicate release')
        require(identity(backup / name) == entry['identity'], f'Original changed before release: {name}')
        require(metadata(target / name) == entry['metadata'], f'Destination metadata changed before release: {name}')
        require(digest(target / name) == entry['sha256'], f'Destination changed before release: {name}')
        record['release_intent'] = name
        write_receipt(receipt, record)
        (backup / name).unlink()
        sync_dir((backup / name).parent)
        record['removed_duplicates'].append(name)
        write_receipt(receipt, record)
    for name, entry in sorted(baseline.items(), key=lambda item: len(Path(item[0]).parts), reverse=True):
        if entry['directory']:
            (backup / name).rmdir()
    sync_dir(source.parent)
    verify_copy(target, baseline)
    record['phase'] = 'complete'
    record.pop('release_intent', None)
    record['completed_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    record['source_volume_free_bytes'] = shutil.disk_usage(source.parent).free
    record['target_volume_free_bytes'] = shutil.disk_usage(target).free
    write_receipt(receipt, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'Run as root so all consumers can be checked')
    require(os.path.ismount('/data'), '/data is not mounted')
    require(SOURCE.stat().st_dev != Path('/var/lib').stat().st_dev, 'Recovery must move to another filesystem')
    require(Path('/var/lib').stat().st_dev == Path('/').stat().st_dev, 'Unexpected target filesystem')
    with open('/run/lock/aurora-partial-recovery-relocation.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = subprocess.check_output(['systemctl', 'show', 'aurora-radar-relocation-stage-20261024.service',
                                         '--property=ActiveState,MainPID'], text=True)
        require('MainPID=0' in state.splitlines() and 'ActiveState=inactive' in state.splitlines(),
                'Wait for the existing radar stage before copying recovery')
        no_open_users(SOURCE)
        baseline = inventory(SOURCE, hashes=args.apply)
        files = {name: entry for name, entry in baseline.items() if not entry['directory']}
        require(len(files) == EXPECTED_FILES and sum(e['identity'][2] for e in files.values()) == EXPECTED_BYTES,
                'Recovery inventory differs from the reviewed 104-file bundle')
        require(baseline['.']['identity'][6] == 0 and
                all(e['identity'][6] in {0, 56781} and e['identity'][7] == 56781 for e in baseline.values()),
                'Recovery ownership differs from the inspected bundle')
        require(set(files) == {'manifest.jsonl'} | {n for n in files if n.endswith('.zst')}, 'Unexpected recovery payload')
        rows = [json.loads(line) for line in (SOURCE / 'manifest.jsonl').read_text().splitlines()]
        require(len(rows) == 206, 'Recovery manifest does not contain 103 completed pairs')
        for name in files.keys() - {'manifest.jsonl'}:
            records = [r for r in rows if r.get('recovery') == str(SOURCE / name)]
            require(len(records) == 2 and [r['action'] for r in records] == ['verified_lossless_copy', 'relocated_partial'],
                    f'Recovery is not complete: {name}')
            require(all(r['compressed_bytes'] == files[name]['identity'][2] for r in records), 'Compressed size differs')
        require(shutil.disk_usage('/var/lib').free >= EXPECTED_BYTES + 64 * 1024**3, 'Keep 64 GiB free on root')
        if args.apply:
            record = relocate(SOURCE, TARGET, RECEIPT, baseline)
            print(json.dumps({key: value for key, value in record.items() if key != 'files'}))
        else:
            print(json.dumps({'source': str(SOURCE), 'target': str(TARGET), 'files': len(files),
                              'bytes': EXPECTED_BYTES, 'target_free_bytes': shutil.disk_usage('/var/lib').free,
                              'apply': False}))


if __name__ == '__main__':
    main()
