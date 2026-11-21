import hashlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
import validate_native_lv1 as validator


def test_parser_and_independent_frame_counts_must_agree(tmp_path, monkeypatch):
    source = tmp_path / 'sample.LV1'
    source.write_bytes(b'radar')
    extension = tmp_path / 'data.so'
    extension.write_bytes(b'parser')
    fake = ModuleType('rpgpy')
    fake_data = ModuleType('rpgpy.data')
    fake_version = ModuleType('rpgpy.version')
    fake_version.__version__ = 'test'
    fake_data.__file__ = str(extension)
    fake.data = fake_data
    fake.read_rpg = lambda path: ({'FileCode': 889348}, {'Time': [1, 2], 'MSec': [3, 4]})
    monkeypatch.setitem(sys.modules, 'rpgpy', fake)
    monkeypatch.setitem(sys.modules, 'rpgpy.data', fake_data)
    monkeypatch.setitem(sys.modules, 'rpgpy.version', fake_version)
    frames = {'samples': 2, 'first_rpg_time': (1, 3), 'last_rpg_time': (2, 4), 'file_code': 889348}
    monkeypatch.setattr(validator, 'check_frames', lambda path: frames)
    sha = hashlib.sha256(b'radar').hexdigest()
    proof = validator.validate(source, 'a' * 32, sha)
    assert proof['rpgpy_read_completed'] is True and proof['fast_archive_authorized'] is False
    frames['samples'] = 3
    with pytest.raises(ValueError, match='parser and native frame'):
        validator.validate(source, 'a' * 32, sha)
