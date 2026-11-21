"""Check RPG native sample framing without expanding large spectral arrays.

This is a preliminary structural check, not a complete validation of sample
payloads and not by itself evidence that a Windows source is finalized.
"""
import os
from pathlib import Path
import stat
import struct

MAGIC = {
    789346: 'LV0', 889346: 'LV0', 1889346: 'LV0',
    789345: 'LV1', 789347: 'LV1', 889347: 'LV1',
    1889347: 'LV1', 889348: 'LV1', 1889348: 'LV1',
}


def check_frames(path):
    path = Path(path)
    level = path.suffix.removeprefix('.')
    if level not in {'LV0', 'LV1'}:
        raise ValueError('not a native LV0 or LV1 file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError('source is not a regular single-link file')
        def read_exact(count):
            value = stream.read(count)
            if len(value) != count:
                raise ValueError('truncated RPG record')
            return value
        code, header_size = struct.unpack('<ii', read_exact(8))
        if MAGIC.get(code) != level:
            raise ValueError('RPG file code does not match native level')
        if not 0 < header_size <= min(before.st_size - 12, 16 * 1024 * 1024):
            raise ValueError('invalid RPG header length')
        stream.seek(8 + header_size)
        samples, = struct.unpack('<i', read_exact(4))
        if samples <= 0 or samples > (before.st_size - stream.tell()) // 16:
            raise ValueError('invalid RPG sample count')
        first = last = None
        for _ in range(samples):
            start = stream.tell()
            size, seconds, milliseconds = struct.unpack('<iIi', read_exact(12))
            if size < 12 or start + 4 + size > before.st_size or not 0 <= milliseconds < 1000:
                raise ValueError('invalid or truncated RPG sample frame')
            if first is None:
                first = (seconds, milliseconds)
            last = (seconds, milliseconds)
            stream.seek(start + 4 + size)
        if stream.tell() != before.st_size:
            raise ValueError('RPG sample frames do not end at EOF')
        after = os.fstat(stream.fileno())
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, field) != getattr(after, field) for field in fields):
            raise ValueError('source changed during framing check')
        return {'file_code': code, 'level': level, 'size': before.st_size,
                'samples': samples, 'first_rpg_time': first, 'last_rpg_time': last,
                'payloads_validated': False, 'finalization_proven': False}
