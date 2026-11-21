"""Build an immutable root-worker bundle from an authenticated retry record.

This is a pure construction step. Its caller must verify prior-worker evidence,
hold coordinator locks, install under the protected recovery root and separately
authenticate the systemd invocation before accepting any outcome.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import PurePosixPath
import re

import retry_launcher


ROOT = '/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/direct-staged-v1'
PLAN_SHA256 = 'e9534ca975fd24517bc0db56fcb9a5bb268c2e16b5d07ce29a5b9fa6621d8bcb'


def build(intent, inputs, launcher_arguments, *, resume_sha256, worker_sha256, now):
    """Return exact files and argv for an already-reserved worker attempt."""
    if not isinstance(intent, dict) or intent.get('schema') != 'radar-gws-retry-attempt-v1':
        raise ValueError('invalid retry intent')
    operation = intent.get('operation_id')
    attempt = intent.get('attempt')
    if (not isinstance(operation, str) or not re.fullmatch('[0-9a-f]{32}', operation)
            or type(attempt) is not int or not 1 <= attempt <= 999999
            or intent.get('retention_authorized') is not False or intent.get('eviction_authorized') is not False):
        raise ValueError('retry identity or authority invalid')
    if not isinstance(inputs, dict) or set(inputs) != {'continuity', 'observation', 'request', 'launcher'}:
        raise ValueError('retry inputs incomplete')
    if any(not isinstance(value, bytes) or not value for value in inputs.values()):
        raise ValueError('retry input bytes missing')
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()}
    if hashes != intent.get('input_sha256'):
        raise ValueError('retry inputs differ from durable intent')
    for value in (resume_sha256, worker_sha256):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
            raise ValueError('recovery module pin missing')
    if type(now) not in (int, float):
        raise ValueError('clock missing')
    regenerated = retry_launcher.build(**launcher_arguments, operation_id=operation, attempt=attempt)
    if (regenerated['launcher'] != inputs['launcher'] or regenerated['request'] != inputs['request']
            or regenerated['unit'] != intent.get('unit') or regenerated['expected'] != intent.get('expected')):
        raise ValueError('regenerated launcher differs from recorded attempt')
    try:
        observation = json.loads(inputs['observation'])
        continuity = json.loads(inputs['continuity'])
        checked = datetime.fromisoformat(continuity['checked_at'].replace('Z', '+00:00'))
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('invalid fresh controls') from None
    expected = intent['expected']
    if (not isinstance(observation, dict)
            or set(observation) != {'relative_path', 'size', 'mtime_seconds', 'observed_at'}
            or observation['relative_path'] != expected['relative_path'] or observation['size'] != expected['size']
            or observation['mtime_seconds'] != expected['mtime_ns'] // 10**9
            or type(observation['observed_at']) not in (int, float)
            or not 0 <= now - observation['observed_at'] <= 600):
        raise ValueError('source observation is stale or differs')
    if (checked.tzinfo is None or not 0 <= now - checked.astimezone(timezone.utc).timestamp() <= 600
            or continuity.get('accepted') is not True or continuity.get('files') != 640
            or continuity.get('plan_sha256') != PLAN_SHA256 or continuity.get('release_authorized') is not False
            or continuity.get('fresh_payload_hash') is not False):
        raise ValueError('continuity evidence stale or differs')
    destination = str(PurePosixPath(ROOT) / f'retry-{operation}-{attempt:06d}')
    files = {name + ('.py' if name == 'launcher' else '.json'): value for name, value in inputs.items()}
    argv = ['/usr/bin/python3', destination + '/launcher.py']
    for name in ('request', 'continuity', 'observation'):
        argv += ['--' + name, destination + '/' + name + '.json', '--' + name + '-sha256', hashes[name]]
    argv += ['--resume-module-sha256', resume_sha256, '--worker-module-sha256', worker_sha256, '--apply']
    files['launcher-args.json'] = (json.dumps(argv, separators=(',', ':')) + '\n').encode()
    files['SHA256SUMS'] = ''.join(f'{hashlib.sha256(raw).hexdigest()}  {name}\n'
                                  for name, raw in sorted(files.items())).encode()
    return {'destination': destination, 'unit': intent['unit'], 'argv': argv, 'files': files,
            'retention_authorized': False, 'eviction_authorized': False}
