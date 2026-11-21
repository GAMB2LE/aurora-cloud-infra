import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
from gws_only_evidence import finalized_source


def test_finalized_source_binds_version_and_observation_age():
    expected = {'relative_path': 'Y2026/M11/D13/example.LV1', 'size': 10, 'mtime_ns': 50, 'sha256': 'a'*64}
    handoff = {'schema': 'radar-protected-stage-handoff-v1', 'source': expected['relative_path'],
               'size': 10, 'source_mtime_ns': 50, 'sha256': 'a'*64, 'stage_identity': [1, 2, 10, 3, 4, 1],
               'stage_operation': 'b'*32, 'post_stage_operation': 'c'*32, 'finalization_invocation_id': 'd'*32,
               'post_stage_observed_at': 1000, 'checked_at': 1001, 'fast_archive_authorized': False,
               'retention_authorized': False, 'eviction_authorized': False}
    stamp = datetime.datetime.fromtimestamp(1300, datetime.timezone.utc).isoformat()
    preflight = {'at': stamp, 'handoff': handoff}
    assert finalized_source(expected, preflight)
    assert not finalized_source({**expected, 'sha256': 'e'*64}, preflight)
    assert not finalized_source(expected, {**preflight, 'at': datetime.datetime.fromtimestamp(1301, datetime.timezone.utc).isoformat()})
    assert not finalized_source(expected, {**preflight, 'handoff': {**handoff, 'retention_authorized': True}})
