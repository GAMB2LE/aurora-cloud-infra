"""One source-to-GWS backlog transfer, with no local payload staging.

This is a state machine, not an executable deployment. A reviewed adapter must
provide fresh source observations, capacity and hold checks, lock ownership,
no-replace GWS admission, pinned commands and independent readback. Its journal
is not an archive ledger or authority to release or prune data.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import time

import direct_pipeline_bounded as transport
import direct_interrupt
from gws_receive import PATH, SHA, MAX_SIZE

MAX_OBSERVATION_AGE = 1200
SETTLE_SECONDS = 21600


def _validate_observation(observation, now):
    if not isinstance(observation, dict) or set(observation) != {'relative_path', 'size', 'mtime_seconds', 'observed_at'}:
        raise ValueError('invalid observation')
    p, size, mtime, stamp = (observation[k] for k in ('relative_path', 'size', 'mtime_seconds', 'observed_at'))
    if not (isinstance(p, str) and PATH.fullmatch(p) and type(size) is int and 0 < size <= MAX_SIZE
            and type(mtime) is int and type(stamp) in (int, float)
            and 0 <= now-stamp <= MAX_OBSERVATION_AGE and 0 <= mtime <= now-SETTLE_SECONDS):
        raise ValueError('stale, unsettled, or invalid observation')


def _validate_source(observation, proof):
    if not isinstance(proof, dict) or set(proof) != {'relative_path', 'identity', 'sha256'}:
        raise ValueError('invalid source proof')
    identity = proof['identity']
    if not (proof['relative_path'] == observation['relative_path'] and isinstance(identity, list)
            and len(identity) == 9 and all(type(x) is int and x >= 0 for x in identity)
            and identity[2] == observation['size'] and identity[3]//10**9 == observation['mtime_seconds']
            and stat.S_ISREG(identity[7]) and identity[8] == 1
            and isinstance(proof['sha256'], str) and SHA.fullmatch(proof['sha256'])):
        raise ValueError('source proof differs from observation')


def _save_exclusive(path, value):
    path = Path(path)
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':'))+'\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(parent)
    finally: os.close(parent)
    return hashlib.sha256(raw).hexdigest()


def transfer_one(adapter, observation, journal_path, *, clock=time.time, monotonic=time.monotonic):
    """Transfer one version, then request independent destination readback.

    A preflight or transport journal left behind always requires reconciliation;
    even a successful stream cannot be replayed. Adapter methods raise on any
    uncertainty. No object-copy or immutable archive claim is made here.
    """
    journal = Path(journal_path)
    preflight = Path(str(journal)+'.preflight.json')
    readback = Path(str(journal)+'.readback.json')
    _validate_observation(observation, clock())
    with direct_interrupt.protect(), adapter.exclusive_recovery_locks():
        deadline = monotonic() + 7200
        adapter.set_deadline(deadline)
        def remaining():
            value = deadline - monotonic()
            if value <= 0: raise TimeoutError('direct-transfer operation budget exhausted')
            return value
        if any(os.path.lexists(p) for p in (preflight, journal, readback)):
            raise ValueError('existing attempt requires reconciliation')
        adapter.require_controls_capacity_and_hold()
        adapter.require_source_observation(observation)
        source = adapter.hash_settled_source(observation['relative_path'], timeout=remaining())
        _validate_source(observation, source)
        # Recheck after the potentially long source hash and immediately before
        # transport. A changed source or route cannot inherit the old proof.
        _validate_observation(observation, clock())
        adapter.require_source_observation(observation)
        adapter.require_source_identity(source)
        adapter.require_local_absence(observation['relative_path'])
        adapter.require_gws_absence_and_capacity(observation['relative_path'], observation['size'])
        adapter.require_controls_capacity_and_hold()
        adapter.require_source_identity(source)
        adapter.require_gws_absence_and_capacity(observation['relative_path'], observation['size'])
        nonce = secrets.token_hex(16)
        expected = {'relative_path': observation['relative_path'], 'size': observation['size'],
                    'mtime_ns': source['identity'][3], 'sha256': source['sha256']}
        sender, receiver, source_raw, receiver_raw = adapter.direct_commands(expected, source['identity'], nonce)
        if not isinstance(source_raw, bytes) or not isinstance(receiver_raw, bytes):
            raise ValueError('unpinned transport scripts')
        operation = {'nonce': nonce, 'source_script_sha256': hashlib.sha256(source_raw).hexdigest(),
                     'receiver_script_sha256': hashlib.sha256(receiver_raw).hexdigest(), 'destination': 'gws-canonical'}
        _save_exclusive(preflight, {'schema': 'radar-direct-backlog-preflight-v1',
            'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'observation': observation,
            'source': source, 'expected': expected, 'operation': operation,
            'eviction_authorized': False, 'retention_authorized': False})
        result = transport.transfer(sender, receiver, expected, journal, operation=operation, timeout=remaining())
        # Independent readback is a separate failure point; if interrupted, the
        # transfer remains an explicitly reconcilable non-authorizing attempt.
        adapter.require_source_identity(source)
        proof = adapter.independent_gws_readback(expected, timeout=remaining())
        remaining()
        if not (isinstance(proof, dict) and proof.get('relative_path') == expected['relative_path']
                and proof.get('size') == expected['size'] and proof.get('mtime_ns') == expected['mtime_ns']
                and proof.get('sha256') == expected['sha256'] and proof.get('stable') is True):
            raise ValueError('independent GWS proof differs')
        _save_exclusive(readback, {'schema': 'radar-direct-backlog-gws-readback-v1',
            'expected': expected, 'proof': proof, 'stream_result': result,
            'object_verified': False, 'immutable_backing_proven': False,
            'eviction_authorized': False, 'retention_authorized': False})
        return {'expected': expected, 'gws_verified': True, 'object_verified': False,
                'immutable_backing_proven': False, 'eviction_authorized': False, 'retention_authorized': False}
