#!/usr/bin/env python3
"""Receive one expected radar version into GWS without primary-disk staging.

This is a transfer primitive, not a complete archive controller. The caller
must durably journal intent, validate a settled stable source, independently
verify both destinations and reconcile any uncertain outcome. A successful
receipt does not establish immutability or permit retention or eviction.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

GWS_RAW = '/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/rpgfmcw94'
GWS_CANARY = '/gws/ssde/j25b/gamb2le/data/internal/aurora-cloud/radar-direct-canaries/v1'
PATH = re.compile(r'Y\d{4}/M\d{2}/D\d{2}/[A-Za-z0-9_.-]+\.(?:LV0|LV0\.NC|LV1|LV1\.NC)\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
MAX_SIZE = 4 * 1024**3


def _dir_id(st):
    return st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid


def receive(base, relative, size, mtime_ns, expected_sha, source, *, nonce=None):
    if not (isinstance(relative, str) and PATH.fullmatch(relative)
            and type(size) is int and 0 < size <= MAX_SIZE
            and type(mtime_ns) is int and 0 <= mtime_ns < 2**63
            and isinstance(expected_sha, str) and SHA.fullmatch(expected_sha)):
        raise ValueError('invalid expected version')
    nonce = nonce or secrets.token_hex(16)
    if not re.fullmatch(r'[0-9a-f]{32}', nonce):
        raise ValueError('invalid operation nonce')
    base = Path(base)
    if not base.is_absolute() or '..' in base.parts:
        raise ValueError('invalid destination root')
    parts = (*base.parts[1:], *Path(relative).parts[:-1])
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds = [os.open('/', flags)]
    names = []
    partial = None
    try:
        for index, part in enumerate(parts):
            if index >= len(base.parts) - 1:
                try:
                    os.mkdir(part, 0o750, dir_fd=fds[-1])
                    os.fsync(fds[-1])
                except FileExistsError:
                    pass
            fd = os.open(part, flags, dir_fd=fds[-1]); fds.append(fd)
            names.append((part, _dir_id(os.fstat(fd))))
        root_id = _dir_id(os.fstat(fds[0]))
        def check_dirs():
            if _dir_id(os.stat('/', follow_symlinks=False)) != root_id:
                raise ValueError('root changed')
            for parent, child, (name, identity) in zip(fds, fds[1:], names):
                if _dir_id(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity or _dir_id(os.fstat(child)) != identity:
                    raise ValueError('destination directory changed')
        check_dirs(); directory = fds[-1]; name = Path(relative).name
        partial = '.' + name + '.' + nonce + '.partial'
        out = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        digest = hashlib.sha256(); count = 0
        with os.fdopen(out, 'wb') as f:
            while True:
                chunk = source.read(min(1024**2, size - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > size:
                    raise ValueError('source exceeds expected size')
                f.write(chunk); digest.update(chunk)
            if count != size or digest.hexdigest() != expected_sha:
                raise ValueError('source bytes differ')
            f.flush(); os.fchmod(f.fileno(), 0o660)
            os.utime(f.fileno(), ns=(mtime_ns, mtime_ns)); os.fsync(f.fileno())
            if os.fstat(f.fileno()).st_mtime_ns != mtime_ns:
                raise ValueError('destination timestamp differs')
        check_dirs()
        # link is atomic and refuses an existing canonical name. Any conflict
        # or interruption leaves the uniquely named partial for reconciliation.
        os.link(partial, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        os.fsync(directory)
        os.unlink(partial, dir_fd=directory); partial = None; os.fsync(directory)
        return {'schema': 'radar-direct-gws-receipt-v1', 'relative_path': relative,
                'size': size, 'mtime_ns': mtime_ns, 'sha256': expected_sha,
                'atomic_no_replace': True, 'source_stability_proven': False,
                'independent_readback': False, 'immutable_backing_proven': False,
                'eviction_authorized': False, 'retention_authorized': False}
    finally:
        for fd in reversed(fds):
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('relative'); parser.add_argument('size', type=int)
    parser.add_argument('mtime_ns', type=int); parser.add_argument('sha256')
    parser.add_argument('nonce')
    parser.add_argument('--canary', action='store_true', help='Use the separate fixed canary namespace')
    args = parser.parse_args()
    print(json.dumps(receive(GWS_CANARY if args.canary else GWS_RAW, args.relative, args.size, args.mtime_ns,
                             args.sha256, sys.stdin.buffer, nonce=args.nonce), sort_keys=True))


if __name__ == '__main__':
    main()
