import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
from stage_from_journal import load_capture
from windows_native_probe import durable_write


def test_load_capture_requires_protected_complete_journal(tmp_path):
    root = tmp_path / 'journal'
    root.mkdir(mode=0o700)
    op = 'a' * 32
    directory = root / op
    directory.mkdir(mode=0o700)
    for name, raw in [('intent', '{"operation":"' + op + '"}'), ('observation', '{}'),
                      ('attestation', '{}'), ('complete', '{"cleanup_confirmed":true}')]:
        durable_write(directory / (name + '.json'), raw.encode())
    assert load_capture(root, op, owner=os.getuid())['intent']['operation'] == op
    directory.chmod(0o750)
    with pytest.raises(ValueError, match='not protected'):
        load_capture(root, op, owner=os.getuid())


def test_journal_symlink_is_refused(tmp_path):
    root = tmp_path / 'journal'
    root.mkdir(mode=0o700)
    target = tmp_path / 'target'
    target.mkdir(mode=0o700)
    (root / ('a' * 32)).symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match='not protected'):
        load_capture(root, 'a' * 32, owner=os.getuid())
