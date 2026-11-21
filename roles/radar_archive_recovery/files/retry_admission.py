"""Authenticate and parse a completed read-only staged-file admission worker."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import time
import math

import retry_launcher
import retry_observer


def _read_root_owned(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
                or before.st_mode & 0o022 or before.st_size > 1024 * 1024):
            raise ValueError('unsafe admission launcher')
        raw = stream.read(1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('admission launcher changed')
    return raw


def _launcher_hash(path):
    return hashlib.sha256(_read_root_owned(path)).hexdigest()


def _fresh_observation(argv, expected, now):
    if argv.count('--observation') != 1 or argv.count('--observation-sha256') != 1:
        raise ValueError('observation arguments missing or ambiguous')
    try:
        path = Path(argv[argv.index('--observation') + 1])
        wanted = argv[argv.index('--observation-sha256') + 1]
    except (IndexError, TypeError):
        raise ValueError('observation arguments incomplete') from None
    if not path.is_absolute() or '..' in path.parts or not re.fullmatch('[0-9a-f]{64}', wanted):
        raise ValueError('observation path or digest invalid')
    raw = _read_root_owned(path)
    if hashlib.sha256(raw).hexdigest() != wanted:
        raise ValueError('observation digest differs')
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('observation is not valid JSON') from None
    if (not isinstance(value, dict) or set(value) != {'relative_path', 'size', 'mtime_seconds', 'observed_at'}
            or value['relative_path'] != expected.get('relative_path') or value['size'] != expected.get('size')
            or type(value['mtime_seconds']) is not int or type(expected.get('mtime_ns')) is not int
            or value['mtime_seconds'] != expected['mtime_ns'] // 10**9
            or type(value['observed_at']) not in (int, float) or not math.isfinite(value['observed_at'])
            or not 0 <= now - value['observed_at'] <= 600):
        raise ValueError('observation is stale or differs from expected version')
    return wanted


def accepted(identity, argv, launcher_path, launcher_sha256, expected, *, now=None):
    """Return proofs only from the exact pinned successful service invocation."""
    if os.geteuid() != 0:
        raise ValueError('root is required to authenticate admission')
    unit, invocation, pid = identity.get('unit'), identity.get('invocation_id'), identity.get('main_pid')
    if not (isinstance(unit, str) and re.fullmatch(r'aurora-radar-readonly-admission-[a-z0-9-]+\.service', unit)
            and isinstance(invocation, str) and re.fullmatch('[0-9a-f]{32}', invocation)
            and type(pid) is int and pid > 0 and isinstance(argv, list) and len(argv) >= 2
            and argv[:2] == ['/usr/bin/python3', str(launcher_path)] and '--apply' not in argv
            and re.fullmatch('[0-9a-f]{64}', launcher_sha256)):
        raise ValueError('invalid admission identity')
    before = retry_observer._query(unit)
    if not (before['LoadState'] != 'not-found' and before['ActiveState'] == 'active'
            and before['SubState'] == 'exited' and before['Result'] == 'success'
            and before['ExecMainStatus'] == '0' and before['ExecMainPID'] == str(pid)
            and before['InvocationID'] == invocation
            and 'argv[]=' + shlex.join(argv) + ' ;' in before['ExecStart']):
        raise ValueError('admission worker is not the recorded successful invocation')
    if retry_observer._pids(before['ControlGroup']):
        raise ValueError('admission worker descendants remain')
    if _launcher_hash(Path(launcher_path)) != launcher_sha256:
        raise ValueError('admission launcher digest differs')
    observation_digest = _fresh_observation(argv, expected, time.time() if now is None else now)
    log = subprocess.run(['/bin/journalctl', '-u', unit, '_SYSTEMD_INVOCATION_ID=' + invocation,
                          '-o', 'cat', '--no-pager'], capture_output=True, check=True, timeout=60).stdout
    if len(log) > 2 * 1024 * 1024:
        raise ValueError('admission journal too large')
    # sudo and SSH diagnostics can share the invocation journal. Only one
    # complete single-line JSON record with the admission result is accepted.
    records = []
    for line in log.splitlines():
        if not line.startswith(b'{'):
            continue
        try:
            candidate = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(candidate, dict) and candidate.get('mode') == 'read-only':
            records.append(retry_launcher.admission_proofs(line, expected))
    if len(records) != 1:
        raise ValueError('admission proof missing or ambiguous')
    if retry_observer._query(unit) != before or _launcher_hash(Path(launcher_path)) != launcher_sha256:
        raise ValueError('admission worker or launcher changed during verification')
    if _fresh_observation(argv, expected, time.time() if now is None else now) != observation_digest:
        raise ValueError('admission observation changed during verification')
    return records[0]
