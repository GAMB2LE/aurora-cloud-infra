from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
import finalized_fresh_controller as controller


def test_archive_locks_are_exclusive(tmp_path):
    with controller.archive_locks(tmp_path) as fds:
        assert len(fds) == 2
        with pytest.raises(BlockingIOError):
            with controller.archive_locks(tmp_path): pass
    with controller.archive_locks(tmp_path): pass


def test_unupgraded_coordinator_blocks_fresh_start(monkeypatch):
    monkeypatch.setattr(controller.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        stdout='ActiveState=active\nSubState=running\nMainPID=1\nExecStart=old release\n'))
    with pytest.raises(ValueError, match='compatible'):
        controller.require_r7()
