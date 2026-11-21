"""Create the existing GWS capture and pending-object registration command.

The command runs as the archive owner, not root.  The existing capture module
authenticates the worker, hold, pinned launcher, commit/fence and full readback.
"""
import ast
import hashlib
import json


def program(capture, expected_sha256):
    if not isinstance(capture, bytes) or hashlib.sha256(capture).hexdigest() != expected_sha256:
        raise ValueError('capture launcher differs from reserved attempt')
    source = capture.decode('utf-8')
    trailer = '''
import sys
from contextlib import nullcontext
from per_file_queue import writer_lock
from gws_pending_ledger import register_captured
identity, launcher_sha, expected = json.loads(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
owned = sys.argv[4] == 'coordinator-owned'
if sys.argv[4] not in ('coordinator-owned', 'standalone'):
    raise ValueError('invalid lock ownership')
with (nullcontext() if owned else writer_lock(BASE/'per-file-coordinator.lock')), (nullcontext() if owned else writer_lock(BASE/'bounded-backlog-coordinator.lock')):
    proof = capture(identity, launcher_sha)
    if proof.get('expected') != expected:
        raise ValueError('captured version differs from queue')
    registered = register_captured(PREPARATION, BASE/'gws-only-pending.sqlite')
    if registered.get('relative_path') != expected['relative_path'] or registered.get('state') != 'object-copy-pending':
        raise ValueError('pending object registration differs')
    print(json.dumps({'expected': proof['expected'], 'registered': registered}, sort_keys=True))
'''
    result = source + '\n' + trailer
    ast.parse(result)
    return result


def parse_result(raw, expected):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('capture result is not one JSON record') from None
    if not isinstance(value, dict) or not isinstance(expected, dict):
        raise ValueError('capture result or expected version is invalid')
    registration = value.get('registered', {})
    if not isinstance(registration, dict):
        raise ValueError('capture registration is invalid')
    if (value.get('expected') != expected or registration.get('relative_path') != expected['relative_path']
            or registration.get('state') != 'object-copy-pending'
            or registration.get('retention_authorized') is not False
            or registration.get('eviction_authorized') is not False):
        raise ValueError('capture result differs from expected version')
    return value
