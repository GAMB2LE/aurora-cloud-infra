import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
from read_stage_handoff import read


def test_handoff_is_protected_exact_and_fresh(tmp_path):
    op, after = 'a'*32, 'b'*32
    directory = tmp_path / op
    directory.mkdir(mode=0o700)
    value = {'schema': 'radar-protected-stage-handoff-v1', 'stage_operation': op, 'post_stage_operation': after,
             'source': 'Y2026/M11/D13/example.LV1', 'size': 10, 'sha256': 'c'*64,
             'source_mtime_ns': 123, 'stage_identity': [1, 2, 10, 4, 5, 1], 'checked_at': 1000, 'post_stage_observed_at': 1000,
             'finalization_invocation_id': 'd'*32, 'fast_archive_authorized': False,
             'retention_authorized': False, 'eviction_authorized': False}
    path = directory / ('delivery-handoff-' + after + '.json')
    path.write_text(json.dumps(value))
    path.chmod(0o440)
    assert read(tmp_path, op, after, now=1300, owner=os.getuid()) == value
    with pytest.raises(ValueError, match='stale'):
        read(tmp_path, op, after, now=1301, owner=os.getuid())
    path.chmod(0o640)
    with pytest.raises(ValueError, match='protected'):
        read(tmp_path, op, after, now=1001, owner=os.getuid())
