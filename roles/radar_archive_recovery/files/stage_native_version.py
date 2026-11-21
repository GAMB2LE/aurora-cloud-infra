"""Create a durable, no-overwrite copy of a preliminarily eligible radar version.

The caller must authenticate the Windows observations and eligibility record.
This stage does not validate the full native payload or authorize archiving.
An interrupted operation is retained for reconciliation; it is never retried
by overwriting an existing path.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from windows_native_probe import validate_pair
from windows_observation_gate import WINDOWS_UNIX_EPOCH_TICKS, staging_eligibility_from_captures


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _dir_identity(info):
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise ValueError('not a directory')
    return fd


def stage(source_root, stage_root, eligibility, operation, *, captures, minimum_free_bytes, now=None):
    if (not isinstance(eligibility, dict) or eligibility.get('schema') != 'radar-fast-staging-eligible-v1'
            or eligibility.get('fast_archive_authorized') is not False
            or eligibility.get('retention_authorized') is not False or eligibility.get('eviction_authorized') is not False
            or not isinstance(operation, str) or not re.fullmatch('[0-9a-f]{32}', operation)
            or type(minimum_free_bytes) is not int or minimum_free_bytes < 0
            or type(eligibility.get('size')) is not int or not 0 < eligibility['size'] <= 4 * 1024**3
            or type(eligibility.get('mtime_ticks')) is not int or eligibility['mtime_ticks'] <= WINDOWS_UNIX_EPOCH_TICKS
            or type(eligibility.get('checked_at')) is not int
            or not isinstance(eligibility.get('first_operation'), str) or not re.fullmatch('[0-9a-f]{32}', eligibility['first_operation'])
            or not isinstance(eligibility.get('second_operation'), str) or not re.fullmatch('[0-9a-f]{32}', eligibility['second_operation'])
            or eligibility['first_operation'] == eligibility['second_operation']
            or not isinstance(eligibility.get('first_attestation_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', eligibility['first_attestation_sha256'])
            or not isinstance(eligibility.get('second_attestation_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', eligibility['second_attestation_sha256'])
            or not isinstance(eligibility.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', eligibility['sha256'])):
        raise ValueError('invalid staging eligibility or operation')
    now = int(time.time()) if now is None else now
    if type(now) is not int or not 0 <= now - eligibility['checked_at'] <= 300:
        raise ValueError('staging eligibility is stale or in the future')
    if (not isinstance(captures, list) or len(captures) != 2
            or staging_eligibility_from_captures(*captures, now=eligibility['checked_at']) != eligibility):
        raise ValueError('staging eligibility is not bound to authenticated captures')
    validate_pair(eligibility.get('source'), eligibility.get('successor'))
    parts = eligibility['source'].split('/')
    roots = []
    source_fd = output_fd = stage_fd = None
    try:
        source_fd = _directory(Path(source_root))
        roots.append(source_fd)
        directory_identities = [_dir_identity(os.fstat(source_fd))]
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=roots[-1])
            roots.append(child)
            directory_identities.append(_dir_identity(os.fstat(child)))
        stage_fd = _directory(Path(stage_root))
        stage_info = os.fstat(stage_fd)
        if stage_info.st_uid != os.geteuid() or stage_info.st_mode & 0o022:
            raise ValueError('staging root is not protected')
        available = os.fstatvfs(stage_fd).f_bavail * os.fstatvfs(stage_fd).f_frsize
        if available < eligibility['size'] + minimum_free_bytes:
            raise ValueError('insufficient staging capacity')
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=roots[-1])
        with os.fdopen(file_fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size != eligibility['size']
                    or before.st_mtime_ns != (eligibility['mtime_ticks'] - WINDOWS_UNIX_EPOCH_TICKS) * 100):
                raise ValueError('ASS source identity differs from observed Windows version')
            os.mkdir(operation, 0o750, dir_fd=stage_fd)
            os.fsync(stage_fd)
            output_fd = os.open(operation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=stage_fd)
            os.fchown(output_fd, -1, stage_info.st_gid)
            os.fsync(output_fd)
            intent = {'schema': 'radar-fast-staging-intent-v1', 'operation': operation,
                      'eligibility': eligibility, 'retention_authorized': False, 'eviction_authorized': False}
            _write_json(output_fd, 'intent.json', intent)
            partial_name = '.' + parts[-1] + '.partial'
            fd = os.open(partial_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=output_fd)
            digest = hashlib.sha256(); count = 0
            with os.fdopen(fd, 'wb') as target:
                while True:
                    block = source.read(1024 * 1024)
                    if not block: break
                    if target.write(block) != len(block): raise OSError('short staging write')
                    digest.update(block); count += len(block)
                target.flush(); os.fchmod(target.fileno(), 0o440); os.fchown(target.fileno(), -1, stage_info.st_gid)
                os.fsync(target.fileno())
            after = os.fstat(source.fileno())
            path_after = os.stat(parts[-1], dir_fd=roots[-1], follow_symlinks=False)
            root_after = os.stat(source_root, follow_symlinks=False)
            if _dir_identity(root_after) != directory_identities[0]:
                raise ValueError('source root changed during staging')
            for index, part in enumerate(parts[:-1]):
                current = os.stat(part, dir_fd=roots[index], follow_symlinks=False)
                if (_dir_identity(current) != directory_identities[index+1]
                        or _dir_identity(os.fstat(roots[index+1])) != directory_identities[index+1]):
                    raise ValueError('source directory changed during staging')
            if (_identity(before) != _identity(after) or _identity(before) != _identity(path_after)
                    or count != eligibility['size'] or digest.hexdigest() != eligibility['sha256']):
                raise ValueError('source changed or staging checksum differs')
            os.link(partial_name, parts[-1], src_dir_fd=output_fd, dst_dir_fd=output_fd, follow_symlinks=False)
            os.fsync(output_fd)
            os.unlink(partial_name, dir_fd=output_fd)
            os.fsync(output_fd)
            staged = os.stat(parts[-1], dir_fd=output_fd, follow_symlinks=False)
            proof = {'schema': 'radar-native-stage-v1', 'operation': operation,
                     'source': eligibility['source'], 'successor': eligibility['successor'],
                     'size': count, 'sha256': digest.hexdigest(),
                     'source_identity': list(_identity(before)), 'stage_identity': list(_identity(staged)),
                     'stage_path': str(Path(stage_root) / operation / parts[-1]),
                     'first_operation': eligibility['first_operation'], 'second_operation': eligibility['second_operation'],
                     'first_attestation_sha256': eligibility['first_attestation_sha256'],
                     'second_attestation_sha256': eligibility['second_attestation_sha256'],
                     'payloads_validated': False, 'fast_archive_authorized': False,
                     'retention_authorized': False, 'eviction_authorized': False}
            _write_json(output_fd, 'stage-proof.json', proof)
            return proof
    finally:
        if output_fd is not None: os.close(output_fd)
        if stage_fd is not None: os.close(stage_fd)
        for fd in reversed(roots): os.close(fd)


def _write_json(directory_fd, name, value):
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    os.fsync(directory_fd)
