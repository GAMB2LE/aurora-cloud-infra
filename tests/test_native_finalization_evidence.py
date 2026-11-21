import sys
import hashlib
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
from native_finalization_evidence import assess, assess_captures, RPGPY_EXTENSION, VALIDATOR_SCRIPT, FRAME_SCRIPT
from windows_observation_gate import WINDOWS_UNIX_EPOCH_TICKS, TICKS_PER_SECOND, staging_eligibility_from_captures
from windows_native_probe import job_script, task_parts

SOURCE = 'Y2026/M11/D13/rpgfmcw94-aurora_261113_215959_P01_ZEN.LV1'
NEXT = 'Y2026/M11/D13/rpgfmcw94-aurora_261113_225959_P01_ZEN.LV1'


def obs(op, when):
    return {'schema': 'radar-windows-observation-v1', 'operation': op * 32, 'ok': True,
            'source': SOURCE, 'successor': NEXT, 'size': 10,
            'mtime_ticks': WINDOWS_UNIX_EPOCH_TICKS + 996000 * TICKS_PER_SECOND,
            'successor_size': 20, 'successor_mtime_ticks': WINDOWS_UNIX_EPOCH_TICKS + 999000 * TICKS_PER_SECOND,
            'sha256': 'd' * 64, 'observed_at': when}


def evidence():
    first, second, after = obs('a', 1_000_000), obs('b', 1_000_600), obs('c', 1_000_701)
    identity = [1, 2, 10, 3, 1_000_700 * 10**9, 1]
    stage = {'schema': 'radar-native-stage-v1', 'operation': 'e' * 32, 'source': SOURCE,
             'successor': NEXT, 'size': 10, 'sha256': 'd' * 64, 'first_operation': first['operation'],
             'second_operation': second['operation'], 'stage_identity': identity,
             'source_identity': [1, 2, 10, 996000 * 10**9, 3, 1],
             'stage_path': '/protected/' + 'e' * 32 + '/' + SOURCE.rsplit('/', 1)[-1], 'payloads_validated': False,
             'fast_archive_authorized': False, 'retention_authorized': False, 'eviction_authorized': False}
    structure = {'schema': 'radar-lv1-structure-observation-v1', 'stage_operation': stage['operation'],
                 'sha256': 'd' * 64, 'size': 10, 'samples': 5, 'rpgpy_version': '0.15.13',
                 'rpgpy_extension_sha256': RPGPY_EXTENSION, 'validator_script_sha256': VALIDATOR_SCRIPT,
                 'frame_script_sha256': FRAME_SCRIPT, 'frame_boundaries_verified': True,
                 'rpgpy_read_completed': True, 'fast_archive_authorized': False,
                 'retention_authorized': False, 'eviction_authorized': False}
    current = {'identity': identity, 'sha256': 'd' * 64, 'protected': True}
    return first, second, after, stage, structure, current


def test_complete_evidence_still_does_not_authorize_worker():
    result = assess(*evidence(), now=1_000_702)
    assert result['fast_archive_authorized'] is False
    assert result['retention_authorized'] is result['eviction_authorized'] is False


def test_changed_source_unprotected_stage_or_wrong_parser_is_refused():
    first, second, after, stage, structure, current = evidence()
    with pytest.raises(ValueError, match='post-stage'):
        assess(first, second, dict(after, sha256='f' * 64), stage, structure, current, now=1_000_702)
    with pytest.raises(ValueError, match='protected stage'):
        assess(first, second, after, stage, structure, dict(current, protected=False), now=1_000_702)
    with pytest.raises(ValueError, match='structure proof'):
        assess(first, second, after, stage, dict(structure, rpgpy_read_completed=False), current, now=1_000_702)


def test_final_evidence_binds_three_task_attestations():
    first, second, after, stage, structure, current = evidence()
    def capture(proof):
        operation = proof['operation']
        intent = {'schema': 'radar-windows-probe-intent-v1', 'operation': operation,
                  'source': SOURCE, 'successor': NEXT,
                  'job_sha256': hashlib.sha256(job_script(operation, SOURCE, NEXT).encode()).hexdigest(),
                  'retention_authorized': False, 'eviction_authorized': False}
        name, arguments, _ = task_parts(intent)
        attestation = {'schema': 'radar-windows-task-attestation-v1', 'operation': operation,
                       'task': name, 'job_sha256': intent['job_sha256'],
                       'arguments_sha256': hashlib.sha256(arguments.encode()).hexdigest(),
                       'principal': 'aurora', 'logon_type': 'Interactive', 'run_level': 'Limited',
                       'last_result': 0, 'last_run_utc': '1970-01-01T00:00:00Z',
                       'receipt_sha256': hashlib.sha256(json.dumps(proof, sort_keys=True,
                           separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()}
        return {'intent': intent, 'attestation': attestation, 'observation': proof,
                'complete': {'cleanup_confirmed': True}}
    a, b, c = capture(first), capture(second), capture(after)
    eligibility = staging_eligibility_from_captures(a, b, now=1_000_700)
    stage.update(first_attestation_sha256=eligibility['first_attestation_sha256'],
                 second_attestation_sha256=eligibility['second_attestation_sha256'])
    result = assess_captures(a, b, c, stage, structure, current, now=1_000_702)
    assert len(result['post_stage_attestation_sha256']) == 64
    assert result['fast_archive_authorized'] is False
    c['observation'] = dict(after, size=11)
    with pytest.raises(ValueError, match='attestation'):
        assess_captures(a, b, c, stage, structure, current, now=1_000_702)
