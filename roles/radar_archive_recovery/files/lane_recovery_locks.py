"""Share the legacy recovery guard across isolated GWS and object lanes.

Legacy workers take the same guard exclusively, so they still exclude both
lanes. Each new lane also holds its own exclusive lock; relocation remains
protected by the existing shared relocation lock. Callers must authenticate
the other worker and all destination fences separately.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

LOCK_ROOT = Path('/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/radar-delivery-lanes-v1')


@contextmanager
def locks(base, helper, singleton, lane, *, lock_root=LOCK_ROOT, owner=0):
    if lane not in {'gws', 'object'}:
        raise ValueError('invalid recovery lane')
    singleton = Path(singleton); lock_root = Path(lock_root)
    base.safe(singleton); base.protected(lock_root)
    directory = lock_root.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != owner or directory.st_mode & 0o022:
        raise ValueError('lane lock directory is not protected')
    fds = []
    try:
        for path, operation in ((singleton, fcntl.LOCK_SH), (lock_root / (lane + '.lock'), fcntl.LOCK_EX)):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            fds.append(fd)
            before, current = os.fstat(fd), path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
                raise ValueError('recovery lock identity differs')
            if path.parent == lock_root and (before.st_uid != owner or before.st_mode & 0o077):
                raise ValueError('lane lock is not protected')
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        with helper.lock(False):
            for fd, path in zip(fds, (singleton, lock_root / (lane + '.lock'))):
                opened, current = os.fstat(fd), path.lstat()
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    raise ValueError('recovery lock replaced')
            yield
    finally:
        for fd in reversed(fds): os.close(fd)
