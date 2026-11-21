"""Stream a protected staged version; the caller must authorize destination admission.

This module does not perform admission or authorize archival, deletion or eviction.
The caller must not accept a remote destination unless both this sender and the
independent destination verification succeed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import zlib

from inspect_protected_stage import identity, inspect


def stream(root, operation, expected, output, *, owner=0):
    proof, _ = inspect(root, operation, owner=owner)
    if (not isinstance(expected, dict) or set(expected) != {'relative_path', 'size', 'sha256'}
            or expected != {'relative_path': proof['source'], 'size': proof['size'], 'sha256': proof['sha256']}):
        raise ValueError('requested source version differs from protected stage')
    result = stream_version(root, operation, expected, proof['stage_identity'], output, owner=owner)
    current, _ = inspect(root, operation, owner=owner)
    if current != proof:
        raise ValueError('protected stage proof changed while streaming')
    return result


def stream_version(root, operation, expected, stage_identity, output, *, owner=0):
    """Read-only sender for a version authenticated by the admission caller.

    This entry point requires only group read access to the staged bytes, not
    access to the root-only journals. It never constitutes admission itself.
    """
    if (not isinstance(operation, str) or not re.fullmatch('[0-9a-f]{32}', operation)
            or not isinstance(expected, dict) or set(expected) != {'relative_path', 'size', 'sha256'}
            or not isinstance(expected['relative_path'], str)
            or not re.fullmatch(r'Y\d{4}/M\d{2}/D\d{2}/[A-Za-z0-9_.-]+\.LV1', expected['relative_path'])
            or type(expected['size']) is not int or expected['size'] <= 0
            or not isinstance(expected['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', expected['sha256'])
            or not isinstance(stage_identity, list) or len(stage_identity) != 6
            or any(type(value) is not int for value in stage_identity)
            or stage_identity[2] != expected['size'] or stage_identity[5] != 1):
        raise ValueError('invalid authenticated stage version')
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('invalid stage root')
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    directory_fd = None
    try:
        directory_fd = os.open(operation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        root_info, directory_info = os.fstat(root_fd), os.fstat(directory_fd)
        if any(info.st_uid != owner or info.st_mode & 0o022 for info in (root_info, directory_info)):
            raise ValueError('staging directories are not protected')
        filename = Path(expected['relative_path']).name
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if (identity(before) != stage_identity or before.st_uid != owner
                    or stat.S_IMODE(before.st_mode) != 0o440 or not stat.S_ISREG(before.st_mode)):
                raise ValueError('protected staged source identity differs')
            digest, count, wire = hashlib.sha256(), 0, 0
            compressor = zlib.compressobj(3, wbits=31)
            def send(data):
                nonlocal wire
                view = memoryview(data)
                while view:
                    written = output.write(view)
                    if written is None or written <= 0:
                        raise OSError('short compressed output write')
                    wire += written
                    view = view[written:]
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                count += len(block)
                if count > expected['size']:
                    raise ValueError('staged source grew')
                digest.update(block)
                send(compressor.compress(block))
            send(compressor.flush(zlib.Z_FINISH))
            output.flush()
            if (identity(os.fstat(source.fileno())) != identity(before)
                    or identity(os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)) != identity(before)
                    or count != expected['size'] or digest.hexdigest() != expected['sha256']):
                raise ValueError('staged source changed while streaming')
        current_root = os.stat(root, follow_symlinks=False)
        current_directory = os.stat(operation, dir_fd=root_fd, follow_symlinks=False)
        if ((current_root.st_dev, current_root.st_ino) != (root_info.st_dev, root_info.st_ino)
                or (current_directory.st_dev, current_directory.st_ino) != (directory_info.st_dev, directory_info.st_ino)
                or current_root.st_uid != owner or current_root.st_mode & 0o022
                or current_directory.st_uid != owner or current_directory.st_mode & 0o022):
            raise ValueError('protected staging directory changed while streaming')
        return {'schema': 'radar-direct-source-stream-v1', **expected, 'read_bytes': count,
                'source_stable': True, 'wire_encoding': 'gzip', 'wire_bytes': wire,
                'retention_authorized': False, 'eviction_authorized': False}
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(root_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation')
    parser.add_argument('expected_json')
    parser.add_argument('stage_identity_json')
    args = parser.parse_args()
    receipt = stream_version('/var/lib/aurora-radar-fast-staging', args.operation,
                             json.loads(args.expected_json), json.loads(args.stage_identity_json), sys.stdout.buffer)
    print(json.dumps(receipt, sort_keys=True), file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
