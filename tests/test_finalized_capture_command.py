import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
from capture_gws_only import FINALIZED, finalized_argv


def test_capture_binds_request_and_apply_command(tmp_path):
    raw = b'{"test":true}\n'
    argv = ['/usr/bin/python3', FINALIZED + '/finalized_gws_launcher.py', '--request', '/protected/request.json',
            '--request-sha256', hashlib.sha256(raw).hexdigest(), '--continuity', '/protected/continuity.json',
            '--continuity-sha256', 'a'*64]
    path = tmp_path / 'launcher-args.json'
    path.write_text(json.dumps(argv))
    assert finalized_argv(tmp_path, raw) == argv
    with pytest.raises(ValueError): finalized_argv(tmp_path, raw + b'x')
    argv.append('--apply')
    path.write_text(json.dumps(argv))
    with pytest.raises(ValueError): finalized_argv(tmp_path, raw)
