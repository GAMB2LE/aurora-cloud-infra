from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
from lane_recovery_locks import locks


class Base:
    def safe(self, path): pass
    def protected(self, path): pass


class Helper:
    @contextmanager
    def lock(self, exclusive):
        assert exclusive is False
        yield


def test_lane_parallelism_preserves_legacy_exclusion(tmp_path):
    singleton = tmp_path/'singleton'; singleton.touch(mode=0o600)
    lanes = tmp_path/'lanes'; lanes.mkdir(mode=0o700)
    for name in ('gws.lock', 'object.lock'): (lanes/name).touch(mode=0o600)
    with locks(Base(), Helper(), singleton, 'gws', lock_root=lanes, owner=os.getuid()):
        with locks(Base(), Helper(), singleton, 'object', lock_root=lanes, owner=os.getuid()):
            with pytest.raises(BlockingIOError):
                with locks(Base(), Helper(), singleton, 'gws', lock_root=lanes, owner=os.getuid()): pass
            fd = os.open(singleton, os.O_RDONLY)
            try:
                with pytest.raises(BlockingIOError): fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally: os.close(fd)
    fd = os.open(singleton, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            with locks(Base(), Helper(), singleton, 'object', lock_root=lanes, owner=os.getuid()): pass
    finally: os.close(fd)
