import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
from radar_native_frames import check_frames


def native(path, *, magic=889348):
    header = b'header'
    frame = lambda sec: struct.pack('<iIi', 12, sec, 500) + b'data'
    path.write_bytes(struct.pack('<ii', magic, len(header)) + header + struct.pack('<i', 2)
                     + frame(10) + frame(11))


def test_native_frame_boundaries_and_levels(tmp_path):
    path = tmp_path / 'sample.LV1'
    native(path)
    result = check_frames(path)
    assert result['samples'] == 2 and result['last_rpg_time'] == (11, 500)
    assert result['payloads_validated'] is result['finalization_proven'] is False
    path = path.rename(path.with_suffix('.LV0'))
    with pytest.raises(ValueError, match='file code'):
        check_frames(path)


def test_truncated_or_trailing_data_is_rejected(tmp_path):
    path = tmp_path / 'sample.LV1'
    native(path)
    data = path.read_bytes()
    path.write_bytes(data[:-1])
    with pytest.raises(ValueError, match='truncated|sample count'):
        check_frames(path)
    path.write_bytes(data + b'x')
    with pytest.raises(ValueError, match='EOF'):
        check_frames(path)
