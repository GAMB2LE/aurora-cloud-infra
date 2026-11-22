#!/usr/bin/env python3
"""Stage a fully verified local recovery image without altering live radar paths."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

BASE = Path('/var/lib/aurora-retention-return-20261121')
SOURCE = Path('/data/aurora/raw-relocated/rpgfmcw94')
PROOFS = Path('/var/lib/aurora-cloud/radar-relocation-20261024')
DESTINATION = Path('/data/aurora/recovery/retention-images-20261121')


def run(*args):
    return subprocess.run(args, check=True, timeout=7200)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def attributes(path):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError('unexpected file type or hardlink')
    return {'size': s.st_size, 'mtime_ns': s.st_mtime_ns,
            'mode': stat.S_IMODE(s.st_mode), 'uid': s.st_uid, 'gid': s.st_gid,
            'xattrs': {key: os.getxattr(path, key).hex() for key in os.listxattr(path)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'Y2026/M05/D(?:02|03|04|05|24|26)', args.day):
        raise ValueError('day is outside the assessed relocated cohort')
    stem = args.day.replace('/', '-')
    source = SOURCE / args.day
    proof_path = PROOFS / (stem + '.json')
    proof_bytes = proof_path.read_bytes()
    proof = json.loads(proof_bytes)
    readback = json.loads((BASE / (stem + '-source-readback.json')).read_text())
    if (readback['proof_sha256'] != hashlib.sha256(proof_bytes).hexdigest()
            or readback['day'] != args.day or proof['day'] != args.day):
        raise ValueError('full source readback does not match relocation proof')
    files = proof['files']
    if len(files) != 96 or set(files) != set(readback['files']):
        raise ValueError('expected complete 96-file radar day')
    if source.resolve() != source or source.stat().st_dev != Path('/data').stat().st_dev:
        raise ValueError('unexpected source layout')
    if not os.path.ismount('/data'):
        raise ValueError('data mount unavailable')
    destination = DESTINATION / stem
    DESTINATION.mkdir(mode=0o700, exist_ok=True)
    if destination.exists():
        raise ValueError('staged day already exists; inspect its receipt')
    payload = sum(row['size'] for row in files.values())
    capacity = os.statvfs('/data')
    if capacity.f_bavail * capacity.f_frsize < payload + 55 * 2**30:
        raise ValueError('insufficient worst-case staging space plus 50 GiB reserve')
    def check_source():
        if set(p.name for p in source.iterdir()) != set(files) or proof_path.read_bytes() != proof_bytes:
            raise ValueError('source coverage or relocation proof changed')
        for name, expected in files.items():
            if Path(name).name != name:
                raise ValueError('unsafe receipt filename')
            path = source / name
            if attributes(path) != {k: v for k, v in expected.items() if k != 'sha256'}:
                raise ValueError('source metadata changed: ' + name)
            s = path.lstat()
            identity = [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns,
                        s.st_uid, s.st_gid, s.st_mode, s.st_nlink]
            if identity != readback['files'][name]['signature']:
                raise ValueError('source identity differs from current byte readback')
    with Path('/run/lock/aurora-radar-relocation.lock').open() as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        check_source()
        scratch = Path(tempfile.mkdtemp(prefix='.' + stem + '-', dir=DESTINATION))
        image, view = scratch / 'archive.btrfs', scratch / 'view'
        view.mkdir()
        mounted = False
        try:
            # Sparse image reservation does not preallocate payload-size disk space.
            size = ((payload + 5 * 2**30 + 2**30 - 1) // 2**30) * 2**30
            run('truncate', '-s', str(size), str(image))
            run('mkfs.btrfs', '-q', str(image))
            run('mount', '-t', 'btrfs', '-o', 'loop,compress-force=zstd:3,nodev,nosuid,noexec', str(image), str(view))
            mounted = True
            run('rsync', '-aAX', '--numeric-ids', '--one-file-system', str(source) + '/', str(view) + '/')
            run('sync', '-f', str(view))
            run('mount', '-o', 'remount,ro,nodev,nosuid,noexec', str(view))
            if set(p.name for p in view.iterdir()) != set(files):
                raise ValueError('image coverage differs')
            for index, (name, expected) in enumerate(sorted(files.items()), 1):
                if (attributes(view / name) != {k: v for k, v in expected.items() if k != 'sha256'}
                        or digest(view / name) != expected['sha256']):
                    raise ValueError('image content or metadata differs: ' + name)
                print(json.dumps({'day': args.day, 'image_files_verified': index}), flush=True)
            check_source()
            run('umount', str(view))
            mounted = False
            view.rmdir()
            image.chmod(0o400)
            receipt = {'schema': 'retention-radar-local-recovery-image-v1', 'day': args.day,
                       'files': files, 'source_readback_sha256': digest(BASE / (stem + '-source-readback.json')),
                       'relocation_proof_sha256': hashlib.sha256(proof_bytes).hexdigest(),
                       'image_sha256': digest(image), 'image_bytes': image.stat().st_size,
                       'image_allocated_bytes': image.stat().st_blocks * 512,
                       'source_bytes': payload, 'full_content_and_file_metadata_readback': True,
                       'live_paths_changed': False, 'deletion_authorized': False}
            with (scratch / 'verified.json').open('x') as stream:
                json.dump(receipt, stream, sort_keys=True)
                stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
            with image.open('rb') as stream:
                os.fsync(stream.fileno())
            scratch.rename(destination)
            print(json.dumps({k: v for k, v in receipt.items() if k != 'files'}), flush=True)
        finally:
            if mounted:
                run('umount', str(view))


if __name__ == '__main__':
    main()
