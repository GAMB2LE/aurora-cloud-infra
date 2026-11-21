import hashlib
import json
import sys
from pathlib import Path

import pytest

root = Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'
sys.path.insert(0, str(root))
import retry_capture


def test_capture_program_binds_checksum_and_uses_both_locks():
    raw = b'def capture(identity, launcher_sha):\n    return {}\n'
    code = retry_capture.program(raw, hashlib.sha256(raw).hexdigest())
    assert "writer_lock(BASE/'per-file-coordinator.lock')" in code
    assert "writer_lock(BASE/'bounded-backlog-coordinator.lock')" in code
    assert 'register_captured(PREPARATION' in code
    with pytest.raises(ValueError, match='differs'):
        retry_capture.program(raw + b' ', hashlib.sha256(raw).hexdigest())


def test_capture_result_requires_matching_version_and_no_release_authority():
    expected = {'relative_path': 'Y2026/M11/D03/file.LV1', 'sha256': 'a' * 64}
    good = {'expected': expected, 'registered': {'relative_path': expected['relative_path'],
            'state': 'object-copy-pending', 'retention_authorized': False, 'eviction_authorized': False}}
    assert retry_capture.parse_result(json.dumps(good), expected) == good
    good['registered']['retention_authorized'] = True
    with pytest.raises(ValueError, match='differs'):
        retry_capture.parse_result(json.dumps(good), expected)
    with pytest.raises(ValueError, match='invalid'):
        retry_capture.parse_result('[]', expected)
