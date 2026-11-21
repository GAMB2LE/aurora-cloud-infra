"""Read root-published stage metadata for use by a guarded transfer admission."""
import json
import os
from pathlib import Path
import re
import stat
import time


def read(root, operation, after, *, now=None, owner=0):
    now = time.time() if now is None else now
    if (not isinstance(operation, str) or not re.fullmatch('[0-9a-f]{32}', operation)
            or not isinstance(after, str) or not re.fullmatch('[0-9a-f]{32}', after)):
        raise ValueError('invalid handoff identity')
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('invalid stage root')
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    child = None
    try:
        child = os.open(operation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        dirs = [os.fstat(root_fd), os.fstat(child)]
        if any(info.st_uid != owner or info.st_mode & 0o022 for info in dirs):
            raise ValueError('handoff directories are not protected')
        name = 'delivery-handoff-' + after + '.json'
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=child)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != owner or before.st_gid != dirs[1].st_gid
                    or stat.S_IMODE(before.st_mode) != 0o440 or before.st_nlink != 1 or before.st_size > 16384):
                raise ValueError('handoff file is not protected')
            raw = stream.read(16385)
            after_stat = os.fstat(stream.fileno())
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_uid', 'st_gid', 'st_nlink')
        current = os.stat(name, dir_fd=child, follow_symlinks=False)
        if any(getattr(before, f) != getattr(other, f) for other in (after_stat, current) for f in fields):
            raise ValueError('handoff changed during reading')
        current_dirs = [os.stat(root, follow_symlinks=False), os.stat(operation, dir_fd=root_fd, follow_symlinks=False)]
        if any((old.st_dev, old.st_ino, old.st_uid, old.st_mode) !=
               (new.st_dev, new.st_ino, new.st_uid, new.st_mode) for old, new in zip(dirs, current_dirs)):
            raise ValueError('handoff directory changed')
        value = json.loads(raw)
        identity = value.get('stage_identity')
        if (value.get('schema') != 'radar-protected-stage-handoff-v1' or value.get('stage_operation') != operation
                or value.get('post_stage_operation') != after
                or not isinstance(value.get('source'), str)
                or not re.fullmatch(r'Y\d{4}/M\d{2}/D\d{2}/[A-Za-z0-9_.-]+\.LV1', value['source'])
                or type(value.get('size')) is not int or value['size'] <= 0
                or not isinstance(value.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', value['sha256'])
                or type(value.get('source_mtime_ns')) is not int or value['source_mtime_ns'] <= 0
                or not isinstance(identity, list) or len(identity) != 6 or any(type(x) is not int for x in identity)
                or identity[2] != value['size'] or identity[5] != 1
                or type(value.get('checked_at')) is not int or not 0 <= now - value['checked_at'] <= 300
                or type(value.get('post_stage_observed_at')) is not int
                or not 0 <= now - value['post_stage_observed_at'] <= 300
                or not isinstance(value.get('finalization_invocation_id'), str)
                or not re.fullmatch('[0-9a-f]{32}', value['finalization_invocation_id'])
                or any(value.get(k) is not False for k in ('fast_archive_authorized', 'retention_authorized', 'eviction_authorized'))):
            raise ValueError('handoff contents are invalid or stale')
        return value
    finally:
        if child is not None: os.close(child)
        os.close(root_fd)
