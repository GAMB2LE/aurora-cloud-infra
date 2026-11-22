"""Queue for existing payloads without stealing leases or silently waiting forever."""
import fcntl
import importlib.util
import os
from pathlib import Path
import signal
import threading
import time

import pytest

ROOT = Path(__file__).parents[1] / 'roles/ass_retention/files'


@pytest.fixture
def waiting(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location('window', ROOT / 'aurora-ass-retention-window.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    path = tmp_path / 'lease.lock'; path.touch()
    holder = os.open(path, os.O_RDONLY); fcntl.flock(holder, fcntl.LOCK_SH)
    yield module, path, holder
    os.close(holder)


def test_waits_for_natural_payload_completion(waiting):
    module, path, holder = waiting
    previous = signal.getsignal(signal.SIGALRM)
    def finish():
        time.sleep(0.05); fcntl.flock(holder, fcntl.LOCK_UN)
    thread = threading.Thread(target=finish); thread.start()
    before = time.monotonic()
    fd = module.take_lock(path, os.getuid(), timeout=2)
    try:
        assert time.monotonic() - before >= 0.04
        with path.open() as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
        assert signal.getsignal(signal.SIGALRM) == previous
    finally:
        os.close(fd); thread.join()


def test_timeout_leaves_existing_payload_lease_intact(waiting):
    module, path, holder = waiting
    with pytest.raises(TimeoutError, match='active work'):
        module.take_lock(path, os.getuid(), timeout=1)
    with path.open() as probe:
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_replaced_lock_path_is_rejected_after_wait(waiting):
    module, path, holder = waiting
    def replace_and_finish():
        time.sleep(0.05)
        replacement = path.with_suffix('.replacement'); replacement.touch(); os.replace(replacement, path)
        fcntl.flock(holder, fcntl.LOCK_UN)
    thread = threading.Thread(target=replace_and_finish); thread.start()
    try:
        with pytest.raises(ValueError, match='path changed'):
            module.take_lock(path, os.getuid(), timeout=2)
    finally:
        thread.join()
