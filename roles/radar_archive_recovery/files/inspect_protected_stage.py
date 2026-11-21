"""Independently read back one protected staging version without changing it."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat


def identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink]


def inspect(root, operation, *, owner=0):
    if not isinstance(operation, str) or not re.fullmatch('[0-9a-f]{32}', operation):
        raise ValueError('invalid stage operation')
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('staging root must be an absolute path')
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    operation_fd = None
    try:
        root_before = os.fstat(root_fd)
        if root_before.st_uid != owner or root_before.st_mode & 0o022:
            raise ValueError('staging root is not protected')
        operation_fd = os.open(operation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        directory_before = os.fstat(operation_fd)
        if directory_before.st_uid != owner or directory_before.st_mode & 0o022:
            raise ValueError('stage operation directory is not protected')
        proof_fd = os.open('stage-proof.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=operation_fd)
        with os.fdopen(proof_fd, 'rb') as stream:
            p = os.fstat(stream.fileno())
            if not stat.S_ISREG(p.st_mode) or p.st_uid != owner or p.st_mode & 0o077 or p.st_nlink != 1 or p.st_size > 1024*1024:
                raise ValueError('unsafe stage proof')
            raw = stream.read(1024*1024+1)
            if identity(p) != identity(os.fstat(stream.fileno())):
                raise ValueError('stage proof changed while reading')
        proof = json.loads(raw)
        path = Path(proof.get('stage_path', ''))
        if (proof.get('schema') != 'radar-native-stage-v1' or proof.get('operation') != operation
                or path.parent != root / operation or not path.name.endswith('.LV1')
                or proof.get('fast_archive_authorized') is not False
                or proof.get('retention_authorized') is not False or proof.get('eviction_authorized') is not False):
            raise ValueError('stage proof path or authority differs')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=operation_fd)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != owner or before.st_gid != root_before.st_gid
                    or stat.S_IMODE(before.st_mode) != 0o440
                    or before.st_nlink != 1 or identity(before) != proof.get('stage_identity')):
                raise ValueError('staged file is not the protected recorded version')
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024*1024), b''):
                digest.update(block)
            after = os.fstat(stream.fileno())
        current = os.stat(path.name, dir_fd=operation_fd, follow_symlinks=False)
        current_dir = os.stat(operation, dir_fd=root_fd, follow_symlinks=False)
        current_root = os.stat(root, follow_symlinks=False)
        if (identity(before) != identity(after) or identity(before) != identity(current)
                or (directory_before.st_dev, directory_before.st_ino) != (current_dir.st_dev, current_dir.st_ino)
                or (root_before.st_dev, root_before.st_ino) != (current_root.st_dev, current_root.st_ino)
                or digest.hexdigest() != proof.get('sha256')):
            raise ValueError('staged version changed during readback')
        return proof, {'identity': identity(before), 'sha256': digest.hexdigest(), 'protected': True}
    finally:
        if operation_fd is not None: os.close(operation_fd)
        os.close(root_fd)
