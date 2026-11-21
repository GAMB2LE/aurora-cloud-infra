"""Read-only systemd and publication-artifact evidence for a GWS retry.

Call as root while the coordinator owns its writer locks. Unknown or changing
worker state raises; no missing observation is treated as absence.
"""
import os
from pathlib import Path
import re
import shlex
import subprocess


JOURNALS = Path('/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/direct-staged-v1')
FENCE = Path('/etc/aurora/gws-direct-publication.fence')
PROPERTIES = ('LoadState', 'ActiveState', 'SubState', 'Result', 'ExecMainStatus', 'ExecMainPID',
              'InvocationID', 'ControlGroup', 'ExecStart')


def _query(unit):
    result = subprocess.run(['/bin/systemctl', 'show', unit, *['-p' + item for item in PROPERTIES]],
                            capture_output=True, text=True, check=True, timeout=30)
    lines = result.stdout.splitlines()
    if any('=' not in line for line in lines):
        raise ValueError('malformed systemd properties')
    pairs = [line.split('=', 1) for line in lines]
    if len({name for name, _ in pairs}) != len(pairs):
        raise ValueError('duplicate systemd properties')
    props = dict(pairs)
    if set(props) != set(PROPERTIES):
        raise ValueError('incomplete systemd properties')
    return props


def _pids(group):
    if not group:
        return []
    if not group.startswith('/system.slice/') or '..' in Path(group).parts:
        raise ValueError('unsafe systemd cgroup')
    root = Path('/sys/fs/cgroup') / group.lstrip('/')
    if not root.is_dir() or root.is_symlink():
        raise ValueError('worker cgroup unavailable')
    pids = set()
    def walk_error(error):
        raise error
    for directory, subdirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        if any((Path(directory) / name).is_symlink() for name in subdirs):
            raise ValueError('symlink in worker cgroup')
        if 'cgroup.procs' not in files:
            raise ValueError('worker cgroup process list unavailable')
        for value in (Path(directory) / 'cgroup.procs').read_text().split():
            pids.add(int(value))
    return sorted(pids)


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def collect(identity, argv, operation):
    """Authenticate worker command and read exact journal/fence artifacts."""
    if os.geteuid() != 0:
        raise ValueError('root is required for complete evidence')
    if not isinstance(identity, dict):
        raise ValueError('worker identity missing')
    unit, invocation, pid = identity.get('unit'), identity.get('invocation_id'), identity.get('main_pid')
    if not (isinstance(unit, str) and re.fullmatch(r'aurora-direct-[a-z0-9-]+\.service', unit)
            and isinstance(invocation, str) and re.fullmatch('[0-9a-f]{32}', invocation)
            and type(pid) is int and pid > 0
            and isinstance(argv, list) and len(argv) > 1 and all(isinstance(x, str) and x for x in argv)
            and isinstance(operation, str) and re.fullmatch(r'20261027-gws-only-(?:20\d{6}t)?\d{6}z-0', operation)):
        raise ValueError('invalid worker or operation identity')
    before = _query(unit)
    if (before['LoadState'] == 'not-found' or before['InvocationID'] != invocation
            or before['ExecMainPID'] != str(pid)
            or 'argv[]=' + shlex.join(argv) + ' ;' not in before['ExecStart']):
        raise ValueError('worker command or identity differs')
    pids = _pids(before['ControlGroup'])
    log = subprocess.run(['/bin/journalctl', '-u', unit, '_SYSTEMD_INVOCATION_ID=' + invocation,
                          '-o', 'cat', '--no-pager'], capture_output=True, text=True, check=True, timeout=60).stdout
    if not log.strip() or len(log) > 1024 * 1024:
        raise ValueError('worker journal missing or too large')
    if not JOURNALS.is_dir() or JOURNALS.is_symlink() or not FENCE.parent.is_dir() or FENCE.parent.is_symlink():
        raise ValueError('publication evidence directory unavailable')
    stem = JOURNALS / ('transfer-' + operation + '.jsonl')
    artifacts = {'commit': _present(Path(str(stem) + '.commit.jsonl')),
                 'fence': _present(FENCE),
                 'fence_reconciliation': _present(Path(str(stem) + '.fence-reconciliation.json')),
                 'readback': _present(Path(str(stem) + '.readback.json'))}
    after = _query(unit)
    if before != after:
        raise ValueError('worker changed during observation')
    return {'actual_worker': {'unit': unit, **after}, 'worker_pids': pids,
            'worker_log': log, 'artifacts': artifacts}


def evidence(identity, argv, operation, expected, *, gws_absent, stage_proof,
             source_proof, holds_ok, capacity_ok):
    """Assemble the actual collector output for precommit_retry.classify.

    Destination, source, hold and capacity observations must come from their
    independent live verifiers; unknown values are deliberately passed through.
    """
    observed = collect(identity, argv, operation)
    stem = JOURNALS / ('transfer-' + operation + '.jsonl')

    def read_unchanged(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read(1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
        if (not raw or len(raw) > 1024 * 1024 or
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError('original journal unavailable or changed')
        return raw

    return {'expected': expected, 'preflight_raw': read_unchanged(Path(str(stem) + '.preflight.json')),
            'journal_raw': read_unchanged(stem), 'recorded_identity': identity, **observed,
            'gws_absent': gws_absent, 'stage_proof': stage_proof, 'source_proof': source_proof,
            'holds_ok': holds_ok, 'capacity_ok': capacity_ok}
