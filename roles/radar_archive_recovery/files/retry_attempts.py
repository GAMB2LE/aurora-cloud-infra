"""Durable, append-only identities for staged GWS recovery attempts.

The coordinator owns the enclosing operation lock.  This module never starts a
process or decides whether an earlier attempt is safe to retire.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def _persist(path, value):
    data = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    fd, temporary = tempfile.mkstemp(prefix='.retry-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            existing_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(existing_fd, 'rb') as existing:
                if existing.read(len(data) + 1) != data:
                    raise ValueError('immutable retry record differs') from None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary)


def reserve(root, operation_id, attempt, proof, inputs, release):
    """Persist a deterministic attempt before any admission or worker launch."""
    if not re.fullmatch(r'[0-9a-f]{32}', operation_id) or type(attempt) is not int or not 1 <= attempt <= 999999:
        raise ValueError('invalid retry identity')
    if not isinstance(proof, dict) or proof.get('state') != 'precommit-lock-refusal' or not isinstance(inputs, dict):
        raise ValueError('missing terminal precommit proof')
    if set(inputs) != {'continuity', 'observation', 'request', 'launcher'} or not all(isinstance(v, bytes) and v for v in inputs.values()):
        raise ValueError('incomplete fresh retry inputs')
    if not isinstance(release, str) or not release:
        raise ValueError('pinned worker release required')
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError('attempt root missing or unsafe')
    attempt_dir = root / f'{operation_id}-{attempt:06d}'
    try:
        attempt_dir.mkdir(mode=0o700)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except FileExistsError:
        if not attempt_dir.is_dir() or attempt_dir.is_symlink():
            raise ValueError('unsafe retry attempt directory') from None
    unit = f'aurora-direct-retry-{operation_id}-{attempt:06d}.service'
    proof_raw = (json.dumps(proof, sort_keys=True, separators=(',', ':')) + '\n').encode()
    record = {'schema': 'radar-gws-retry-attempt-v1', 'operation_id': operation_id,
              'attempt': attempt, 'unit': unit, 'original_invocation': proof.get('invocation_id'),
              'original_journal_sha256': proof.get('journal_sha256'), 'release': release,
              'precommit_proof_sha256': hashlib.sha256(proof_raw).hexdigest(),
              'input_sha256': {key: hashlib.sha256(value).hexdigest() for key, value in inputs.items()},
              'expected': proof.get('expected'), 'retention_authorized': False, 'eviction_authorized': False}
    for key, value in inputs.items():
        # Inputs are represented as bytes in immutable JSON to avoid unpinned
        # external paths or locale-dependent decoding.
        _persist(attempt_dir / (key + '.json'), {'sha256': record['input_sha256'][key], 'hex': value.hex()})
    _persist(attempt_dir / 'precommit-proof.json', proof)
    _persist(attempt_dir / 'intent.json', record)
    return record


def record_identity(root, identity):
    """Bind the launched unit once; a different invocation is never adopted."""
    root = Path(root)
    intent = json.loads((root / 'intent.json').read_text())
    if not (isinstance(identity, dict) and identity.get('unit') == intent['unit']
            and isinstance(identity.get('invocation_id'), str)
            and re.fullmatch('[0-9a-f]{32}', identity['invocation_id'])
            and type(identity.get('main_pid')) is int and identity['main_pid'] > 0):
        raise ValueError('retry worker identity differs')
    _persist(root / 'identity.json', identity)


def record_launch_intent(root, unit):
    """Durably record the launch boundary before invoking systemd."""
    root = Path(root)
    intent = json.loads((root / 'intent.json').read_text())
    if unit != intent['unit']:
        raise ValueError('retry launch unit differs')
    _persist(root / 'launch-intent.json', {'unit': unit, 'operation_id': intent['operation_id'],
                                          'attempt': intent['attempt']})


def has_launch_intent(root):
    root = Path(root)
    try:
        fd = os.open(root / 'launch-intent.json', os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, 'rb') as stream:
        value = json.load(stream)
    intent = json.loads((root / 'intent.json').read_text())
    expected = {'unit': intent['unit'], 'operation_id': intent['operation_id'], 'attempt': intent['attempt']}
    if value != expected:
        raise ValueError('retry launch intent differs')
    return True


def defer(root, identity, now):
    """Persist the first retry deadline for this worker without extending it."""
    root = Path(root)
    _, _, recorded = load(root)
    if recorded != identity or not isinstance(now, (int, float)) or isinstance(now, bool) or now < 0:
        raise ValueError('retry deferral identity or clock differs')
    existing = load_deferral(root)
    if existing is not None:
        if existing['identity'] != identity:
            raise ValueError('retry deferral worker differs')
        return existing
    value = {'identity': identity, 'reason': 'delivery-lock-busy', 'retry_at': now + 300}
    _persist(root / 'deferred.json', value)
    return value


def load_deferral(root):
    try:
        fd = os.open(Path(root) / 'deferred.json', os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        value = json.load(stream)
    if (set(value) != {'identity', 'reason', 'retry_at'} or value['reason'] != 'delivery-lock-busy'
            or not isinstance(value['retry_at'], (int, float)) or isinstance(value['retry_at'], bool)):
        raise ValueError('invalid retry deferral')
    return value


def load(root):
    """Reload exactly the reserved bytes; never regenerate a launched attempt."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError('unsafe retry attempt directory')

    def read(name):
        fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            return json.load(stream)

    intent = read('intent.json')
    if intent.get('schema') != 'radar-gws-retry-attempt-v1' or set(intent.get('input_sha256', {})) != {
            'continuity', 'observation', 'request', 'launcher'}:
        raise ValueError('invalid retry intent')
    proof = read('precommit-proof.json')
    proof_raw = (json.dumps(proof, sort_keys=True, separators=(',', ':')) + '\n').encode()
    if hashlib.sha256(proof_raw).hexdigest() != intent.get('precommit_proof_sha256'):
        raise ValueError('retry precommit proof differs')
    inputs = {}
    for key, expected in intent['input_sha256'].items():
        record = read(key + '.json')
        try:
            raw = bytes.fromhex(record['hex'])
        except (KeyError, ValueError, TypeError):
            raise ValueError('retry input invalid') from None
        if not raw or record.get('sha256') != expected or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('retry input checksum differs')
        inputs[key] = raw
    try:
        identity = read('identity.json')
    except FileNotFoundError:
        identity = None
    if identity is not None and (identity.get('unit') != intent.get('unit')
                                 or not re.fullmatch('[0-9a-f]{32}', identity.get('invocation_id', ''))
                                 or type(identity.get('main_pid')) is not int or identity['main_pid'] <= 0):
        raise ValueError('retry identity invalid')
    return intent, inputs, identity
