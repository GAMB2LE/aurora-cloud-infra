"""Authenticate an outstanding GWS-sourced object worker before another GWS admission.

Call only while holding the coordinator and bounded writer locks. This check
does not clear an object guard, authorize a transfer, or relax the GWS fence.
Unknown worker or marker state fails closed.
"""
import json
import fcntl
import os
from pathlib import Path
import re
import stat

from bounded_backlog_runner import state
from gws_object_release import resolve, verify_pins, authenticate_worker
from gws_pending_replay import valid_expected
from prepare_direct_candidate import BASE, run_remote
from reconcile_gws_first_pair import check_retention_and_fence


def read_control(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or before.st_mode & 0o077 or before.st_size > 1024 * 1024):
            raise ValueError('unsafe object isolation evidence')
        raw = stream.read(1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    current = os.stat(path, follow_symlinks=False)
    if any(getattr(before, key) != getattr(other, key) for other in (after, current) for key in fields):
        raise ValueError('object isolation evidence changed')
    return raw, json.loads(raw), tuple(getattr(before, key) for key in fields)


def unchanged(path, raw, identity):
    current_raw, _, current_identity = read_control(path)
    return current_raw == raw and current_identity == identity


def require_locks(base, lock_fds):
    names = ('per-file-coordinator.lock', 'bounded-backlog-coordinator.lock')
    if not isinstance(lock_fds, dict) or set(lock_fds) != set(names):
        raise ValueError('both archive writer lock descriptors are required')
    for name in names:
        fd = lock_fds[name]
        if type(fd) is not int:
            raise ValueError('invalid writer lock descriptor')
        opened = os.fstat(fd)
        path = os.stat(base / name, follow_symlinks=False)
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(path.st_mode)
                or opened.st_uid != os.getuid() or opened.st_mode & 0o077 or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (path.st_dev, path.st_ino)):
            raise ValueError('writer lock identity differs')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def check(candidate, object_operation, *, lock_fds, base=BASE):
    require_locks(base, lock_fds)
    if not valid_expected(candidate):
        raise ValueError('invalid GWS candidate version')
    if any(os.path.lexists(base / name) for name in ('bounded-backlog-unresolved.json', 'gws-first-pair-unresolved.json')):
        raise ValueError('unresolved GWS or pair operation prevents admission')
    check_retention_and_fence()
    marker_path = base / 'gws-pending-object-unresolved.json'
    if not os.path.lexists(marker_path):
        if object_operation is not None:
            raise ValueError('outstanding object operation has no authenticated guard')
        require_locks(base, lock_fds)
        return {'object_isolated': True, 'object_operation': None,
                'retention_authorized': False, 'eviction_authorized': False}
    marker_raw, marker, marker_identity = read_control(marker_path)
    if (not isinstance(marker, dict) or marker.get('schema') != 'radar-gws-pending-object-intent-v1'
            or marker.get('retention_authorized') is not False or marker.get('eviction_authorized') is not False
            or marker.get('object_key_not_yet_known') is not True or not valid_expected(marker.get('expected'))
            or not isinstance(object_operation, dict)
            or not isinstance(object_operation.get('id'), str) or not re.fullmatch('[0-9a-f]{32}', object_operation['id'])
            or not (object_operation.get('lane') == 'object' or object_operation.get('target') == 'object-verified')
            or object_operation.get('expected') != marker['expected']
            or not isinstance(object_operation.get('intent'), dict)
            or object_operation['intent'].get('preparation') != marker.get('preparation')
            or candidate['relative_path'] == marker['expected']['relative_path']):
        raise ValueError('object guard and operation are not safely isolated')
    out = Path(marker['preparation'])
    if out.parent != base or not re.fullmatch(r'direct-object-gzip-preparation-20\d{6}T\d{6}Z', out.name):
        raise ValueError('object preparation path differs')
    admission_raw, admission, admission_identity = read_control(out / 'gws-pending-admission.json')
    request_raw, request, request_identity = read_control(out / 'request.json')
    identity_raw, identity, service_identity = read_control(out / 'service-identity.json')
    expected = marker['expected']
    try:
        source = request['expected_source']
        source_id = source['identity']
        operation_id = request['operation_id']
    except (KeyError, TypeError):
        raise ValueError('object request is incomplete') from None
    if (not isinstance(source_id, list) or len(source_id) < 4
            or source_id[2] != expected['size'] or source_id[3] != expected['mtime_ns']
            or source.get('sha256') != expected['sha256'] or request.get('relative_path') != expected['relative_path']
            or not isinstance(operation_id, str)
            or not re.fullmatch(r'20261027-gzip-next-20\d{6}t\d{6}z-0', operation_id)
            or marker.get('unit') != 'aurora-object-direct-' + operation_id + '.service'
            or admission.get('expected') != expected or admission.get('preparation') != str(out)
            or admission.get('admission') != marker.get('admission')
            or not isinstance(marker.get('admission'), dict) or marker['admission'].get('accepted') is not True
            or not isinstance(marker['admission'].get('invocation_id'), str)
            or not re.fullmatch('[0-9a-f]{32}', marker['admission']['invocation_id'])
            or admission.get('transfer_started') is not False or admission.get('object_verified') is not False
            or admission.get('retention_authorized') is not False or admission.get('eviction_authorized') is not False
            or not isinstance(identity, dict) or identity.get('unit') != marker['unit']
            or not isinstance(identity.get('invocation_id'), str) or not re.fullmatch('[0-9a-f]{32}', identity['invocation_id'])
            or type(identity.get('main_pid')) is not int or identity['main_pid'] <= 0):
        raise ValueError('object worker evidence differs')
    release, argv = resolve(out)
    if release.get('id') not in {'gws-object-vm01', 'gws-object-vm03', 'gws-object-vm01-lanes'}:
        raise ValueError('object worker is not a pinned GWS-source release')
    verify_pins(release, run_remote)
    before = state(marker['unit'])
    if (before.get('LoadState') == 'not-found' or before.get('InvocationID') != identity['invocation_id']
            or before.get('ExecMainPID') != str(identity['main_pid'])
            or (before.get('ActiveState'), before.get('SubState')) not in
               {('active', 'running'), ('active', 'exited'), ('failed', 'failed')}):
        raise ValueError('object worker identity or state is unknown')
    authenticate_worker(marker['unit'], argv, run_remote)
    if (state(marker['unit']) != before or not unchanged(marker_path, marker_raw, marker_identity)
            or not unchanged(out / 'gws-pending-admission.json', admission_raw, admission_identity)
            or not unchanged(out / 'request.json', request_raw, request_identity)
            or not unchanged(out / 'service-identity.json', identity_raw, service_identity)):
        raise ValueError('object worker or its evidence changed during isolation check')
    check_retention_and_fence()
    require_locks(base, lock_fds)
    return {'object_isolated': True, 'object_operation': object_operation.get('id'),
            'object_unit': marker['unit'], 'object_invocation_id': identity['invocation_id'],
            'retention_authorized': False, 'eviction_authorized': False}
