from pathlib import Path
import sys
import json
import subprocess

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
from prepare_finalized_candidate import build_request, FIELD_HASH


def test_request_binds_source_version_and_handoff():
    handoff = {'source': 'Y2026/M11/D13/example.LV1', 'size': 10, 'source_mtime_ns': 12, 'sha256': 'a'*64,
               'stage_operation': 'b'*32, 'post_stage_operation': 'c'*32}
    source = {'identity': [1, 2, 10, 12, 4, 5, 6, 7, 1, 8], 'sha256': 'a'*64}
    op = '20261027-gws-only-20261114t020000z-0'
    request = build_request({'existing': True}, handoff, source, op)
    assert request['finalized_stage']['expected']['mtime_ns'] == 12
    assert request['expected_source'] == source
    with pytest.raises(ValueError, match='differs'):
        build_request({}, handoff, {**source, 'sha256': 'd'*64}, op)
    with pytest.raises(ValueError, match='differs'):
        build_request({}, handoff, source, 'invalid')


def test_finalized_hash_accepts_recent_stable_file_and_rejects_symlink(tmp_path):
    path = tmp_path / 'source.LV1'; path.write_bytes(b'fresh radar bytes')
    result = subprocess.run([sys.executable, '-c', FIELD_HASH, str(path)], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['identity'][2] == len(b'fresh radar bytes')
    link = tmp_path / 'link.LV1'; link.symlink_to(path)
    assert subprocess.run([sys.executable, '-c', FIELD_HASH, str(link)], capture_output=True).returncode != 0
