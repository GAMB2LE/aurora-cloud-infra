from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
from discover_native_lv1 import discover


def test_finds_cross_day_successor_and_ignores_symlink(tmp_path):
    old = tmp_path / 'Y2026/M11/D13'; old.mkdir(parents=True)
    new = tmp_path / 'Y2026/M11/D14'; new.mkdir(parents=True)
    first = old / 'rpgfmcw94-aurora_261113_235959_P01_ZEN.LV1'; first.write_bytes(b'first')
    second = new / 'rpgfmcw94-aurora_261114_005959_P01_ZEN.LV1'; second.write_bytes(b'second')
    (new / 'rpgfmcw94-aurora_261114_015959_P01_ZEN.LV1').symlink_to(second)
    (new / 'rpgfmcw94-aurora_261114_005959_P01_ZEN.LV0').write_bytes(b'raw')
    pairs = discover(tmp_path, now=datetime(2026, 11, 14, 2, 5, tzinfo=timezone.utc))
    assert len(pairs) == 1
    assert pairs[0]['source'].endswith(first.name) and pairs[0]['successor'].endswith(second.name)
    assert pairs[0]['staging_authorized'] is False
