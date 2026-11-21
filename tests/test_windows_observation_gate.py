import sys
import hashlib
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
from windows_observation_gate import WINDOWS_UNIX_EPOCH_TICKS, TICKS_PER_SECOND, staging_eligibility, staging_eligibility_from_captures
from windows_native_probe import job_script, task_parts

SOURCE = 'Y2026/M11/D13/rpgfmcw94-aurora_261113_215959_P01_ZEN.LV1'
NEXT = 'Y2026/M11/D13/rpgfmcw94-aurora_261113_225959_P01_ZEN.LV1'


def observation(operation, at, **changes):
    result = {'schema': 'radar-windows-observation-v1', 'ok': True, 'operation': operation * 32,
              'source': SOURCE, 'successor': NEXT, 'observed_at': at, 'size': 10,
              'mtime_ticks': WINDOWS_UNIX_EPOCH_TICKS + (at - 3600) * TICKS_PER_SECOND,
              'successor_size': 20, 'successor_mtime_ticks': WINDOWS_UNIX_EPOCH_TICKS + at * TICKS_PER_SECOND,
              'sha256': 'a' * 64}
    result.update(changes)
    return result


def test_two_stable_observations_only_authorize_staging():
    first = observation('a', 1_000_000)
    second = observation('b', 1_000_600, mtime_ticks=first['mtime_ticks'])
    result = staging_eligibility(first, second, now=1_000_601)
    assert result['fast_archive_authorized'] is False
    assert result['retention_authorized'] is result['eviction_authorized'] is False


def test_short_interval_changed_bytes_and_stale_evidence_are_refused():
    first = observation('a', 1_000_000)
    second = observation('b', 1_000_599, mtime_ticks=first['mtime_ticks'])
    with pytest.raises(ValueError, match='ten minutes'):
        staging_eligibility(first, second, now=1_000_600)
    second['observed_at'] += 1
    with pytest.raises(ValueError, match='source version'):
        staging_eligibility(first, dict(second, sha256='b' * 64), now=1_000_601)
    with pytest.raises(ValueError, match='stale'):
        staging_eligibility(first, second, now=1_000_901)
    with pytest.raises(ValueError, match='separate observations'):
        staging_eligibility(first, dict(second, operation=first['operation']), now=1_000_601)


def test_recently_modified_source_is_not_ready(tmp_path):
    first = observation('a', 1_000_000)
    second = observation('b', 1_000_600,
                         mtime_ticks=WINDOWS_UNIX_EPOCH_TICKS + 1_000_000 * TICKS_PER_SECOND)
    first['mtime_ticks'] = second['mtime_ticks']
    with pytest.raises(ValueError, match='twenty minutes'):
        staging_eligibility(first, second, now=1_000_601)


def test_staging_requires_task_attestations_bound_to_both_observations():
    first = observation('a', 1_000_000)
    second = observation('b', 1_000_600, mtime_ticks=first['mtime_ticks'])
    def capture(proof):
        intent = {'schema': 'radar-windows-probe-intent-v1', 'operation': proof['operation'],
                  'source': SOURCE, 'successor': NEXT,
                  'job_sha256': hashlib.sha256(job_script(proof['operation'], SOURCE, NEXT).encode()).hexdigest(),
                  'retention_authorized': False, 'eviction_authorized': False}
        name, arguments, _ = task_parts(intent)
        attestation = {'schema': 'radar-windows-task-attestation-v1', 'operation': proof['operation'],
                       'task': name, 'job_sha256': intent['job_sha256'],
                       'arguments_sha256': hashlib.sha256(arguments.encode()).hexdigest(),
                       'principal': 'aurora', 'logon_type': 'Interactive', 'run_level': 'Limited',
                       'last_result': 0, 'last_run_utc': '1970-01-01T00:00:00Z',
                       'receipt_sha256': hashlib.sha256(json.dumps(proof, sort_keys=True,
                           separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()}
        return {'intent': intent, 'attestation': attestation, 'observation': proof,
                'complete': {'cleanup_confirmed': True}}
    a, b = capture(first), capture(second)
    result = staging_eligibility_from_captures(a, b, now=1_000_601)
    assert len(result['first_attestation_sha256']) == 64 and result['fast_archive_authorized'] is False
    b['observation'] = dict(b['observation'], size=11)
    with pytest.raises(ValueError, match='attestation'):
        staging_eligibility_from_captures(a, b, now=1_000_601)
