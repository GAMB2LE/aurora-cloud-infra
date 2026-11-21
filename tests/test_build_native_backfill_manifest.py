import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
from build_native_backfill_manifest import build


def test_frozen_manifest_contains_only_settled_source_files_absent_on_gws():
    now = 2_000_000_000
    def item(name, age, state='absent'):
        return {'name': f'rpgfmcw94-aurora_261113_{name}_P01_ZEN.LV1',
                'size': 100, 'source_mtime': now-age, 'gws': state}
    day = {'date': '2026-11-13', 'source_count': 3,
           'files': [item('010000', 22000), item('020000', 60), item('030000', 22000, 'metadata-matched-checksum-unverified')]}
    raw = (json.dumps(day)+'\n').encode()
    manifest = build(raw, now=now)
    assert manifest['candidate_count'] == 1 and '010000' in manifest['candidates'][0]['relative_path']
    assert manifest['eviction_authorized'] is manifest['retention_authorized'] is False
    with pytest.raises(ValueError, match='duplicate source date'):
        build(raw + raw, now=now)
    day['source_count'] = 4
    with pytest.raises(ValueError, match='incomplete'):
        build((json.dumps(day)+'\n').encode(), now=now)
