"""Conservative preliminary gate for two authenticated Windows observations.

This does not validate native payloads, publish a staged file, or authorize a
fast archive transfer. The caller must authenticate each observation against
its immutable task intent and successful capture before invoking this gate.
"""
import re
import hashlib
import json

from windows_native_probe import task_parts, validate_attestation, validate_pair, validate_proof

WINDOWS_UNIX_EPOCH_TICKS = 621355968000000000
TICKS_PER_SECOND = 10_000_000


def staging_eligibility(first, second, *, now):
    if type(now) is not int:
        raise ValueError('integer UTC clock is required')
    for value in (first, second):
        if (not isinstance(value, dict) or value.get('schema') != 'radar-windows-observation-v1'
                or value.get('ok') is not True or not isinstance(value.get('operation'), str)
                or not re.fullmatch('[0-9a-f]{32}', value['operation'])
                or type(value.get('observed_at')) is not int
                or type(value.get('size')) is not int or value['size'] <= 0
                or type(value.get('mtime_ticks')) is not int or value['mtime_ticks'] <= WINDOWS_UNIX_EPOCH_TICKS
                or type(value.get('successor_size')) is not int or value['successor_size'] <= 0
                or type(value.get('successor_mtime_ticks')) is not int or value['successor_mtime_ticks'] <= WINDOWS_UNIX_EPOCH_TICKS
                or not isinstance(value.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', value['sha256'])):
            raise ValueError('invalid Windows observation')
        validate_pair(value.get('source'), value.get('successor'))
    if first['operation'] == second['operation']:
        raise ValueError('two separate observations are required')
    if any(first[key] != second[key] for key in ('source', 'successor', 'size', 'mtime_ticks', 'sha256')):
        raise ValueError('Windows source version or successor changed')
    if second['observed_at'] - first['observed_at'] < 600:
        raise ValueError('observations are less than ten minutes apart')
    modified = (second['mtime_ticks'] - WINDOWS_UNIX_EPOCH_TICKS) // TICKS_PER_SECOND
    if second['observed_at'] - modified < 1200:
        raise ValueError('Windows source has not been unchanged for twenty minutes')
    if not 0 <= now - second['observed_at'] <= 300:
        raise ValueError('latest Windows observation is stale or in the future')
    return {'schema': 'radar-fast-staging-eligible-v1', 'source': second['source'],
            'successor': second['successor'], 'size': second['size'], 'mtime_ticks': second['mtime_ticks'],
            'sha256': second['sha256'], 'first_operation': first['operation'],
            'second_operation': second['operation'], 'checked_at': now,
            'fast_archive_authorized': False, 'retention_authorized': False, 'eviction_authorized': False}


def staging_eligibility_from_captures(first, second, *, now):
    """Require both durable task attestations before producing staging evidence."""
    observations, attestations = [], []
    for capture in (first, second):
        if not isinstance(capture, dict) or capture.get('complete') != {'cleanup_confirmed': True}:
            raise ValueError('Windows task capture is incomplete')
        intent = capture.get('intent')
        if not isinstance(intent, dict):
            raise ValueError('Windows task intent missing')
        task_parts(intent)
        observation = validate_proof(intent, capture.get('observation'))
        attestation = validate_attestation(intent, observation, capture.get('attestation'))
        observations.append(observation)
        attestations.append(attestation)
    result = staging_eligibility(*observations, now=now)
    result['first_attestation_sha256'], result['second_attestation_sha256'] = (
        hashlib.sha256(json.dumps(item, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        for item in attestations)
    return result
