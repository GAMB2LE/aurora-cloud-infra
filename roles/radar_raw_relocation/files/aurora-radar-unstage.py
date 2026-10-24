#!/usr/bin/env python3
"""Withdraw only this operation's redundant radar stages; preserve every original.

A reviewed request pins the plan, helper and all paired-SHA stage manifests.
Fresh unchanged identities reuse that proof without reading every observation
again. A permanent journal prevents old relocation commands refilling the stage.
"""
if not __debug__:
    raise SystemExit('Run without Python optimization')

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

OWNER = 0
MAX_AGE = 6 * 3600
STAGE_UNITS = ('aurora-radar-relocation-stage-20261024.service',
               'aurora-radar-relocation-stage-d13-20261024.service')


def fail(message):
    raise RuntimeError(message)


def present(path):
    return os.path.lexists(path)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identity(info):
    return [info.st_dev, info.st_ino]


def control_signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_uid, info.st_gid, info.st_mode, info.st_nlink]


def read_owned(path):
    """Read small control files, never raw payloads; reject links and replacement."""
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != OWNER or before.st_mode & 0o022):
        fail('Control file is not owned and immutable to other users: ' + str(path))
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        data = stream.read()
        after = os.fstat(stream.fileno())
    if control_signature(before) != control_signature(after) or control_signature(path.lstat()) != control_signature(before):
        fail('Control file changed while reading: ' + str(path))
    return data


def parse_request(path):
    data = read_owned(path)
    request = json.loads(data)
    if (set(request) != {'operation_id', 'config', 'helper', 'plan_sha256',
                        'helper_sha256', 'manifests'}
            or not re.fullmatch(r'[a-z0-9-]{8,80}', request['operation_id'])
            or not isinstance(request['manifests'], dict)):
        fail('Invalid reviewed withdrawal request')
    for value in [request['plan_sha256'], request['helper_sha256'], *request['manifests'].values()]:
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
            fail('Invalid reviewed SHA-256 pin')
    return request, digest(data)


def load_helper(request):
    path = Path(request['helper'])
    if digest(read_owned(path)) != request['helper_sha256']:
        fail('Helper does not match reviewed pin')
    spec = importlib.util.spec_from_file_location('radar_relocation', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def exclusive_lock(module):
    # No waiting behind a long live sender and no truncation/replacement of its lock.
    with module.LOCK.open('r') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('Relocation lock busy; no withdrawal attempted')
        yield


def stage_idle():
    for unit in STAGE_UNITS:
        result = subprocess.run(['systemctl', 'show', unit, '--property=ActiveState,MainPID,ControlPID,Job'],
                                check=True, capture_output=True, text=True)
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if (fields.get('ActiveState') not in ('inactive', 'failed')
                or fields.get('MainPID') != '0' or fields.get('ControlPID') != '0'
                or fields.get('Job')):
            fail('Stage process or queued start remains: ' + unit)


def mount_records():
    mounts = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        if len(fields) < 10 or '-' not in fields:
            fail('Incomplete mount inventory')
        def decoded(value):
            return Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value))
        major, minor = (int(value) for value in fields[2].split(':'))
        mounts.append({'path': decoded(fields[4]), 'root': decoded(fields[3]),
                       'device': os.makedev(major, minor)})
    if not mounts:
        fail('Empty mount inventory')
    return mounts


def check_topology(module, plan):
    if present(module.ACTIVE):
        fail('Active relocation exists')
    if plan.get('launch_hold') and present(plan['launch_hold']):
        fail('Cutover launch barrier remains')
    module.mount_roots(plan)
    mounts = mount_records()
    for day in plan['days']:
        source, target, quarantine = module.paths(plan, day)
        if present(quarantine) or present(module.progress_path(plan, day)):
            fail('Quarantine or original release evidence exists: ' + day)
        for root, volume in ((source, Path(plan['project_mount'])),
                             (target, Path(plan['data_mount'])),
                             (quarantine, Path(plan['project_mount']))):
            base = [mount for mount in mounts if mount['path'] == volume]
            if len(base) != 1:
                fail('Ambiguous or missing volume mount identity: ' + str(volume))
            selected_root = base[0]['root'] / root.relative_to(volume)
            for mount in mounts:
                if mount == base[0]:
                    continue
                point = mount['path']
                if (point == root or root in point.parents
                        or (point in root.parents and volume in point.parents)):
                    fail('Selected tree has an unexpected mount: ' + str(point))
                # A bind outside the selected paths can still expose these same
                # bytes. Compare the kernel filesystem device and internal root,
                # including aliases of a whole ancestor or any selected subtree.
                alias = mount['root']
                if (mount['device'] == base[0]['device']
                        and (alias == selected_root or alias in selected_root.parents
                             or selected_root in alias.parents)):
                    fail('Selected tree has an external mount alias: ' + str(point))



def closed_users(roots, identities):
    """Fail closed on open users through normal paths or device/inode aliases."""
    def under(path):
        return any(path == str(root) or path.startswith(str(root) + '/') for root in roots)
    for proc in Path('/proc').glob('[0-9]*'):
        if int(proc.name) == os.getpid():
            continue  # Our pinned directory descriptors are deliberately open.
        try:
            handles = list((proc / 'fd').iterdir()) + [proc / 'cwd', proc / 'root']
        except (FileNotFoundError, ProcessLookupError):
            continue
        for handle in handles:
            try:
                link, info = os.readlink(handle), handle.stat()
                if tuple(identity(info)) in identities or under(link):
                    fail('Selected tree has an open user: ' + str(handle))
            except (FileNotFoundError, ProcessLookupError):
                continue
        try:
            lines = (proc / 'maps').read_text().splitlines()
        except (FileNotFoundError, ProcessLookupError):
            continue
        for line in lines:
            fields = line.split(maxsplit=5)
            major, minor = (int(value, 16) for value in fields[3].split(':'))
            key = (os.makedev(major, minor), int(fields[4]))
            if key in identities or (len(fields) == 6 and under(fields[5])):
                fail('Selected tree has a mapped user: ' + str(proc))
    # Permission, malformed maps and other unexpected failures propagate.


def file_set(module, fd):
    records = {}
    directory = os.fstat(fd)
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != directory.st_dev:
            fail('Unexpected file type/link in selected directory: ' + name)
        records[name] = module.signature(info)
    return records


def same_directory(path, fd, expected):
    if (not stat.S_ISDIR(path.lstat().st_mode) or identity(path.lstat()) != expected
            or identity(os.fstat(fd)) != expected):
        fail('Selected directory was replaced: ' + str(path))


def journal_token(path):
    data = read_owned(path)
    return data, control_signature(path.lstat())


def checkpoint(path, record, previous_bytes):
    """Exclusive initial creation, then pinned atomic replacement with fsync."""
    data = (json.dumps(record, sort_keys=True, indent=2) + '\n').encode()
    if previous_bytes is None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        if journal_token(path) != previous_bytes:
            fail('Withdrawal journal changed')
        fd, temporary = tempfile.mkstemp(prefix='.withdrawal-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if journal_token(path) != previous_bytes:
                fail('Withdrawal journal changed before publication')
            os.replace(temporary, path)
        finally:
            if present(temporary):
                os.unlink(temporary)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return journal_token(path)


def proof_bundle(module, plan, request, require_fresh=True):
    if set(request['manifests']) != set(plan['days']):
        fail('Reviewed manifest pins must cover every plan day')
    proofs = {}
    for day in plan['days']:
        content = read_owned(module.manifest_path(plan, day))
        if digest(content) != request['manifests'][day]:
            fail('Stage manifest changed: ' + day)
        proof = json.loads(content)
        names = set(proof['files'])
        if (proof['day'] != day or len(names) != module.expected_files(plan, day)
                or names != set(proof['source_signature']) or names != set(proof['destination_signature'])
                or 'mounted_verification' in proof):
            fail('Invalid or mounted stage proof: ' + day)
        for name in names:
            if name in ('.', '..') or '/' in name or '\x00' in name:
                fail('Unsafe manifest basename')
            if not re.fullmatch(r'[a-f0-9]{64}', proof['files'][name]['sha256']):
                fail('Missing paired SHA proof')
        age = time.time() - proof['verified_at']
        if require_fresh and (age < 0 or age > MAX_AGE):
            fail('Stage proof expired or has a future timestamp: ' + day)
        proofs[day] = proof
    return proofs


def run(module, request, request_sha, *, apply=False, resume=False):
    config = module.safe_path(request['config'])
    plan = module.load(config)
    journal_path = Path(plan['state_root']) / 'stage-withdrawal.json'
    with exclusive_lock(module), ExitStack() as stack:
        config_bytes = read_owned(config)
        if digest(config_bytes) != request['plan_sha256'] or json.loads(config_bytes) != plan:
            fail('Plan does not match reviewed pin')
        if digest(read_owned(Path(request['helper']))) != request['helper_sha256']:
            fail('Helper changed after import')
        check_topology(module, plan)
        stage_idle()
        proofs = proof_bundle(module, plan, request, require_fresh=False)
        opened, directories = {}, {}
        roots, identities = [], set()
        for day in plan['days']:
            source, target, quarantine = module.paths(plan, day)
            roots.extend((source, target, quarantine))
            opened[day], directories[day] = {}, {}
            for kind, path in (('source', source), ('target', target)):
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, fd)
                opened[day][kind] = fd
                directories[day][kind] = identity(os.fstat(fd))
                same_directory(path, fd, directories[day][kind])
                identities.add(tuple(directories[day][kind]))
                identities.update(tuple(sig[:2]) for sig in file_set(module, fd).values())
            if directories[day]['source'] == directories[day]['target']:
                fail('Source and target are the same directory')
        pins = {'request_sha256': request_sha, 'request': request,
                'tool_sha256': digest(read_owned(Path(__file__))), 'directories': directories}
        entries = [[day, name] for day in plan['days'] for name in sorted(proofs[day]['files'])]
        previous = journal_token(journal_path) if present(journal_path) else None
        if previous is None:
            if resume:
                fail('Resume requires the original withdrawal journal')
            journal = {'schema': 1, 'pins': pins, 'completed': [], 'pending': None,
                       'status': 'withdrawing', 'started_at': time.time()}
        else:
            journal = json.loads(previous[0])
            if (set(journal) != {'schema', 'pins', 'completed', 'pending', 'status', 'started_at'}
                    or journal['schema'] != 1 or journal['pins'] != pins
                    or journal['status'] not in ('withdrawing', 'complete')):
                fail('Foreign or malformed withdrawal journal')
            done = journal['completed']
            next_entry = entries[len(done)] if isinstance(done, list) and len(done) < len(entries) else None
            if (not isinstance(done, list) or len(done) > len(entries) or done != entries[:len(done)]
                    or (journal['pending'] is not None and journal['pending'] != next_entry)):
                fail('Invalid withdrawal progress')
            if journal['status'] == 'complete' and (done != entries or journal['pending'] is not None):
                fail('Incomplete completed journal')
            if journal['status'] != 'complete' and not resume:
                fail('Interrupted withdrawal requires explicit --resume')
        def validate():
            if digest(read_owned(config)) != request['plan_sha256']:
                fail('Plan changed during withdrawal')
            if digest(read_owned(Path(request['helper']))) != request['helper_sha256']:
                fail('Helper changed during withdrawal')
            check_topology(module, plan)
            proof_bundle(module, plan, request, require_fresh=journal['status'] != 'complete')
            for day in plan['days']:
                source, target, _ = module.paths(plan, day)
                for kind, path in (('source', source), ('target', target)):
                    same_directory(path, opened[day][kind], directories[day][kind])
                if file_set(module, opened[day]['source']) != proofs[day]['source_signature']:
                    fail('Original source changed or is missing: ' + day)
                expected = dict(proofs[day]['destination_signature'])
                for done_day, name in journal['completed']:
                    if done_day == day:
                        expected.pop(name)
                current = file_set(module, opened[day]['target'])
                pending = journal['pending']
                if pending and pending[0] == day and pending[1] not in current:
                    expected.pop(pending[1])
                if current != expected:
                    fail('Target changed, foreign file, or missing without intent: ' + day)
        validate()  # All days pass together before any journal or unlink.
        closed_users(roots, identities)
        if not apply:
            return {'status': 'validated', 'files': len(entries), 'completed': len(journal['completed'])}
        if journal['status'] == 'complete':
            return {'status': 'already-complete', 'files': len(entries)}
        if previous is None:
            previous = checkpoint(journal_path, journal, None)
        for day, name in entries[len(journal['completed']):]:
            validate()
            if journal_token(journal_path) != previous:
                fail('Withdrawal journal changed before unlink')
            if journal['pending'] is None:
                journal['pending'] = [day, name]
                previous = checkpoint(journal_path, journal, previous)
            validate()
            if journal_token(journal_path) != previous:
                fail('Withdrawal journal changed after intent')
            source, target, _ = module.paths(plan, day)
            # The durable pending record is written first. Recheck both entries
            # immediately before unlinking only the opened target directory.
            for kind, path in (('source', source), ('target', target)):
                same_directory(path, opened[day][kind], directories[day][kind])
            original = os.stat(name, dir_fd=opened[day]['source'], follow_symlinks=False)
            if (module.signature(original) != proofs[day]['source_signature'][name]
                    or not stat.S_ISREG(original.st_mode) or original.st_nlink != 1):
                fail('Original changed immediately before unlink')
            try:
                copy = os.stat(name, dir_fd=opened[day]['target'], follow_symlinks=False)
            except FileNotFoundError:
                copy = None  # Only the exact durable pending entry may be absent.
            if copy is not None:
                if (module.signature(copy) != proofs[day]['destination_signature'][name]
                        or not stat.S_ISREG(copy.st_mode) or copy.st_nlink != 1):
                    fail('Target changed immediately before unlink')
                os.unlink(name, dir_fd=opened[day]['target'])
            os.fsync(opened[day]['target'])
            journal['completed'].append([day, name])
            journal['pending'] = None
            previous = checkpoint(journal_path, journal, previous)
        validate()
        journal['status'] = 'complete'
        checkpoint(journal_path, journal, previous)
        return {'status': 'complete', 'files': len(entries), 'journal': str(journal_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != OWNER:
        fail('Run as root for complete closure and ownership checks')
    request, request_sha = parse_request(args.request)
    module = load_helper(request)
    print(json.dumps(run(module, request, request_sha, apply=args.apply, resume=args.resume), indent=2))


if __name__ == '__main__':
    main()
