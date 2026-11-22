#!/usr/bin/env python3
"""Verify every relocated radar byte against its relocation receipt; never delete."""
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

ROOT = Path('/data/aurora/raw-relocated/rpgfmcw94')
PROOFS = Path('/var/lib/aurora-cloud/radar-relocation-20261024')
OUTPUT = Path('/var/lib/aurora-retention-return-20261121')
LOCK = Path('/run/lock/aurora-radar-relocation.lock')


def signature(path):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError('unexpected source file: ' + str(path))
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns,
            s.st_uid, s.st_gid, s.st_mode, s.st_nlink]


def write(name, record):
    target = OUTPUT / name
    temporary = target.with_suffix('.tmp')
    with temporary.open('x') as stream:
        json.dump(record, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    fd = os.open(OUTPUT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    OUTPUT.mkdir(mode=0o700, exist_ok=True)
    with LOCK.open() as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        for day in sorted(ROOT.glob('Y*/M*/D*')):
            relative = str(day.relative_to(ROOT))
            proof_path = PROOFS / (relative.replace('/', '-') + '.json')
            proof_bytes = proof_path.read_bytes()
            proof = json.loads(proof_bytes)
            if proof['day'] != relative:
                raise ValueError('wrong proof day')
            expected = proof['files']
            before = {p.name: signature(p) for p in day.iterdir()}
            if set(before) != set(expected):
                raise ValueError('source coverage differs: ' + relative)
            verified = {}
            for name, record in sorted(expected.items()):
                path = day / name
                if Path(name).name != name or name in ('.', '..'):
                    raise ValueError('unsafe receipt path')
                s = path.lstat()
                if (s.st_size, s.st_mtime_ns, stat.S_IMODE(s.st_mode), s.st_uid, s.st_gid) != (
                        record['size'], record['mtime_ns'], record['mode'], record['uid'], record['gid']):
                    raise ValueError('metadata changed: ' + str(path))
                if {key: os.getxattr(path, key).hex() for key in os.listxattr(path)} != record['xattrs']:
                    raise ValueError('extended attributes changed: ' + str(path))
                digest = hashlib.sha256()
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino) != tuple(before[name][:2]):
                        raise ValueError('source replaced before opening')
                    for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                        digest.update(block)
                if digest.hexdigest() != record['sha256'] or signature(path) != before[name]:
                    raise ValueError('content or source identity changed: ' + str(path))
                verified[name] = {'sha256': digest.hexdigest(), 'size': record['size'],
                                  'signature': before[name]}
                print(json.dumps({'day': relative, 'verified_files': len(verified),
                                  'expected_files': len(expected), 'last_file': name}), flush=True)
            if ({p.name: signature(p) for p in day.iterdir()} != before
                    or proof_path.read_bytes() != proof_bytes):
                raise ValueError('source or receipt changed during verification')
            result = {'schema': 'relocated-radar-retention-source-readback-v1',
                      'verified_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                      'day': relative, 'proof_sha256': hashlib.sha256(proof_bytes).hexdigest(),
                      'files': verified, 'read_only': True, 'deletion_authorized': False}
            write(relative.replace('/', '-') + '-source-readback.json', result)


if __name__ == '__main__':
    main()
