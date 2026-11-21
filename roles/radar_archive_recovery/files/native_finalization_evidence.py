"""Bind authenticated Windows, staging and LV1 parser observations.

The caller authenticates task intents, stage ownership and parser provenance.
The result is evidence only: it does not admit a worker or authorize retention.
"""
from pathlib import PurePosixPath
import re

import hashlib
import json

from windows_native_probe import task_parts, validate_attestation, validate_proof
from windows_observation_gate import WINDOWS_UNIX_EPOCH_TICKS, staging_eligibility, staging_eligibility_from_captures

RPGPY_EXTENSION = '022b2264e87702c157330b975b71ac2a382a5ccde38e80e4b9479e0d894c8ce2'
VALIDATOR_SCRIPT = '650a51483cd90d841a42d72174327070d384b8e40724efb4ef2ff59c08e91e4a'
VALIDATOR_SCRIPTS = {VALIDATOR_SCRIPT, '772e28ae1be01be95b279795dadd484b5aff5bfaaf648648d3c4eb64243c1bec'}
FRAME_SCRIPT = 'c24ac703582c0c4e09d898bda0bc1ee7a31cc259f7d74ee80542b9213c8c110d'


def assess(first, second, after, stage, structure, stage_readback, *, now):
    if type(now) is not int or not isinstance(stage, dict):
        raise ValueError('invalid finalization clock or stage')
    identity = stage.get('stage_identity')
    if (stage.get('schema') != 'radar-native-stage-v1' or not isinstance(identity, list)
            or len(identity) != 6 or any(type(value) is not int for value in identity)
            or identity[2] <= 0 or identity[5] != 1
            or stage.get('payloads_validated') is not False or stage.get('fast_archive_authorized') is not False
            or stage.get('retention_authorized') is not False or stage.get('eviction_authorized') is not False):
        raise ValueError('invalid protected stage proof')
    stage_time = identity[4] // 10**9
    eligible = staging_eligibility(first, second, now=stage_time)
    source_identity = stage.get('source_identity')
    if (stage.get('first_operation') != first['operation'] or stage.get('second_operation') != second['operation']
            or any(stage.get(key) != eligible[key] for key in ('source', 'successor', 'size', 'sha256'))
            or identity[2] != stage['size']
            or not isinstance(source_identity, list) or len(source_identity) != 6
            or any(type(value) is not int for value in source_identity) or source_identity[2] != stage['size']
            or source_identity[3] != (second['mtime_ticks'] - WINDOWS_UNIX_EPOCH_TICKS) * 100
            or source_identity[5] != 1
            or not isinstance(stage.get('operation'), str) or not re.fullmatch('[0-9a-f]{32}', stage['operation'])
            or not isinstance(stage.get('stage_path'), str)
            or not PurePosixPath(stage['stage_path']).is_absolute() or '..' in PurePosixPath(stage['stage_path']).parts
            or PurePosixPath(stage['stage_path']).parent.name != stage['operation']
            or not stage['source'].endswith('.LV1')
            or PurePosixPath(stage['stage_path']).name != PurePosixPath(stage['source']).name):
        raise ValueError('stage differs from authenticated observations')
    if (not isinstance(after, dict) or after.get('schema') != 'radar-windows-observation-v1'
            or after.get('ok') is not True or not isinstance(after.get('operation'), str)
            or not re.fullmatch('[0-9a-f]{32}', after['operation'])
            or after['operation'] in (first['operation'], second['operation'])
            or any(after.get(key) != second[key] for key in ('source', 'successor', 'size', 'mtime_ticks', 'sha256'))
            or type(after.get('successor_size')) is not int or after['successor_size'] <= 0
            or type(after.get('successor_mtime_ticks')) is not int or after['successor_mtime_ticks'] <= 0
            or type(after.get('observed_at')) is not int or after['observed_at'] < stage_time
            or not 0 <= now - after['observed_at'] <= 300):
        raise ValueError('post-stage Windows observation differs or is stale')
    if (not isinstance(stage_readback, dict) or stage_readback.get('identity') != identity
            or stage_readback.get('sha256') != stage['sha256']
            or stage_readback.get('protected') is not True):
        raise ValueError('current protected stage readback differs')
    if (not isinstance(structure, dict) or structure.get('schema') != 'radar-lv1-structure-observation-v1'
            or structure.get('stage_operation') != stage['operation']
            or structure.get('sha256') != stage['sha256'] or structure.get('size') != stage['size']
            or type(structure.get('samples')) is not int or structure['samples'] <= 0
            or structure.get('rpgpy_version') != '0.15.13'
            or structure.get('rpgpy_extension_sha256') != RPGPY_EXTENSION
            or structure.get('validator_script_sha256') not in VALIDATOR_SCRIPTS
            or structure.get('frame_script_sha256') != FRAME_SCRIPT
            or structure.get('frame_boundaries_verified') is not True or structure.get('rpgpy_read_completed') is not True
            or structure.get('fast_archive_authorized') is not False
            or structure.get('retention_authorized') is not False or structure.get('eviction_authorized') is not False):
        raise ValueError('LV1 structure proof differs or is incomplete')
    return {'schema': 'radar-native-finalized-evidence-v1', 'stage_operation': stage['operation'],
            'source': stage['source'], 'successor': stage['successor'], 'size': stage['size'],
            'sha256': stage['sha256'], 'stage_path': stage['stage_path'], 'stage_identity': identity,
            'first_operation': first['operation'], 'second_operation': second['operation'],
            'post_stage_operation': after['operation'], 'post_stage_observed_at': after['observed_at'],
            'samples': structure['samples'], 'checked_at': now,
            'fast_archive_authorized': False, 'retention_authorized': False, 'eviction_authorized': False}


def assess_captures(first, second, after, stage, structure, stage_readback, *, now):
    """Require authenticated task captures for all three Windows observations."""
    if (not isinstance(stage, dict) or not isinstance(stage.get('stage_identity'), list)
            or len(stage['stage_identity']) != 6 or type(stage['stage_identity'][4]) is not int):
        raise ValueError('stage identity missing')
    stage_time = stage['stage_identity'][4] // 10**9
    eligible = staging_eligibility_from_captures(first, second, now=stage_time)
    if any(stage.get(key) != eligible[key] for key in ('first_attestation_sha256', 'second_attestation_sha256')):
        raise ValueError('stage is not bound to the two task attestations')
    if not isinstance(after, dict) or after.get('complete') != {'cleanup_confirmed': True}:
        raise ValueError('post-stage task capture is incomplete')
    intent = after.get('intent')
    if not isinstance(intent, dict):
        raise ValueError('post-stage task intent missing')
    task_parts(intent)
    post = validate_proof(intent, after.get('observation'))
    attestation = validate_attestation(intent, post, after.get('attestation'))
    result = assess(first['observation'], second['observation'], post, stage, structure, stage_readback, now=now)
    result['first_attestation_sha256'] = eligible['first_attestation_sha256']
    result['second_attestation_sha256'] = eligible['second_attestation_sha256']
    result['post_stage_attestation_sha256'] = hashlib.sha256(json.dumps(
        attestation, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return result
