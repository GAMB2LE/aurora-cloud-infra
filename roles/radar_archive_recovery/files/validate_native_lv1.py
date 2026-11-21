"""Read-only RPG LV1 structure check for a previously hashed staging copy."""
import hashlib
import os
from pathlib import Path
import re
import stat

import radar_native_frames
from radar_native_frames import check_frames


def validate(path, stage_operation, expected_sha256):
    if (not re.fullmatch('[0-9a-f]{32}', stage_operation)
            or not re.fullmatch('[0-9a-f]{64}', expected_sha256)):
        raise ValueError('invalid staging operation or digest')
    path = Path(path)
    if path.suffix != '.LV1':
        raise ValueError('full parser is restricted to LV1')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size <= 0:
            raise ValueError('validation copy is not a regular single-link file')
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != expected_sha256:
        raise ValueError('validation copy differs from staged version')
    frames = check_frames(path)
    import rpgpy
    import rpgpy.data
    from rpgpy.version import __version__ as rpgpy_version
    header, data = rpgpy.read_rpg(path)
    after = os.stat(path, follow_symlinks=False)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(before, key) != getattr(after, key) for key in fields):
        raise ValueError('validation copy changed during parsing')
    times, millis = data['Time'], data['MSec']
    if (len(times) != frames['samples'] or len(millis) != frames['samples']
            or (int(times[0]), int(millis[0])) != tuple(frames['first_rpg_time'])
            or (int(times[-1]), int(millis[-1])) != tuple(frames['last_rpg_time'])
            or int(header['FileCode']) != frames['file_code']):
        raise ValueError('full parser and native frame check differ')
    with open(rpgpy.data.__file__, 'rb') as module:
        validator_sha = hashlib.sha256(module.read()).hexdigest()
    with open(__file__, 'rb') as module:
        script_sha = hashlib.sha256(module.read()).hexdigest()
    with open(radar_native_frames.__file__, 'rb') as module:
        frame_sha = hashlib.sha256(module.read()).hexdigest()
    return {'schema': 'radar-lv1-structure-observation-v1', 'stage_operation': stage_operation,
            'sha256': expected_sha256, 'size': before.st_size, 'samples': frames['samples'],
            'first_rpg_time': list(frames['first_rpg_time']), 'last_rpg_time': list(frames['last_rpg_time']),
            'rpgpy_version': rpgpy_version, 'rpgpy_extension_sha256': validator_sha,
            'validator_script_sha256': script_sha, 'frame_script_sha256': frame_sha,
            'frame_boundaries_verified': True, 'rpgpy_read_completed': True,
            'fast_archive_authorized': False, 'retention_authorized': False, 'eviction_authorized': False}
