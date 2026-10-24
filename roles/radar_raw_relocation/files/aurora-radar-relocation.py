#!/usr/bin/env python3
"""Preserve closed radar days on /data while keeping their logical raw paths."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

GIB = 1024 ** 3
ACTIVE = Path('/etc/aurora/radar-relocation-active.json')
LOCK = Path('/run/lock/aurora-radar-relocation.lock')
HELPER = '/usr/local/lib/aurora-radar-relocation.py'
SYSTEMD_ROOT = Path('/etc/systemd/system')


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.new')
    with temporary.open('w') as out:
        os.fchmod(out.fileno(), 0o644)
        json.dump(value, out, sort_keys=True, indent=2)
        out.write('\n')
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    sync_dir(path.parent)


def signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]


def safe_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Expected an absolute contained path')
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise RuntimeError('Symlink is not a relocation path: ' + str(part))
    return path


def load(path):
    plan = json.loads(safe_path(path).read_text())
    for key in ('raw_root', 'destination_root', 'quarantine_root', 'state_root'):
        plan[key] = str(safe_path(plan[key]))
    roots = [Path(plan[key]) for key in ('raw_root', 'destination_root', 'quarantine_root', 'state_root')]
    if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(roots) for b in roots[i+1:]):
        raise ValueError('Relocation roots must be disjoint')
    if not plan['days'] or len(set(plan['days'])) != len(plan['days']):
        raise ValueError('Expected unique closed radar days')
    if any(not re.fullmatch(r'Y[0-9]{4}/M[0-9]{2}/D[0-9]{2}', day) for day in plan['days']):
        raise ValueError('Unexpected day path')
    for key in ('project_required_free_bytes', 'data_reserve_bytes', 'expected_files_per_day'):
        if not isinstance(plan[key], int) or plan[key] <= 0:
            raise ValueError('Expected positive reservation/count: ' + key)
    overrides = plan.get('expected_files_by_day', {})
    if (not isinstance(overrides, dict) or set(overrides) - set(plan['days'])
            or any(type(count) is not int or count <= 0 for count in overrides.values())):
        raise ValueError('Expected positive counts only for explicitly reviewed days')
    for unit in plan['consumers']:
        if (unit != 'aurora-object-store-recovery-worker@raw.service'
                and not re.fullmatch(r'aurora-[a-z0-9-]+\.service', unit)):
            raise ValueError('Unexpected consumer unit')
    if 'launchers' in plan and plan['launchers'] != ['aurora-object-store-recovery.service']:
        raise ValueError('Unexpected relocation launcher')
    if 'launch_hold' in plan:
        plan['launch_hold'] = str(safe_path(plan['launch_hold']))
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', plan['launch_hold']):
            raise ValueError('Unexpected launch hold path')
    return plan


def expected_files(plan, day):
    return plan.get('expected_files_by_day', {}).get(day, plan['expected_files_per_day'])


def paths(plan, day):
    if day not in plan['days']:
        raise ValueError('Day is outside the reviewed plan')
    return tuple(safe_path(Path(plan[key]) / day) for key in
                 ('raw_root', 'destination_root', 'quarantine_root'))


def manifest_path(plan, day):
    return Path(plan['state_root']) / (day.replace('/', '-') + '.json')


def progress_path(plan, day):
    return Path(plan['state_root']) / (day.replace('/', '-') + '-release.json')


def original_snapshot(plan, day, origin, evidence):
    """A missing original is valid only after its durable release intent exists."""
    progress_file = progress_path(plan, day)
    progress = json.loads(progress_file.read_text()) if progress_file.exists() else {}
    allowed_missing = set(progress.get('released', []))
    if progress.get('pending'):
        allowed_missing.add(progress['pending'])
    current = snapshot(origin, minimum_age=0) if origin.exists() and any(origin.iterdir()) else {}
    expected = evidence['source_signature']
    if set(current) - set(expected) or set(expected) - set(current) - allowed_missing:
        raise RuntimeError('Unexpected original file set: ' + day)
    if any(signature != expected[name] for name, signature in current.items()):
        raise RuntimeError('Original changed since staging: ' + day)
    return current


def snapshot(directory, minimum_age=7 * 86400):
    directory = safe_path(directory)
    if not directory.is_dir() or os.path.ismount(directory):
        raise RuntimeError('Expected an ordinary closed source directory: ' + str(directory))
    records = {}
    for path in sorted(directory.iterdir()):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != directory.stat().st_dev:
            raise RuntimeError('Only ordinary single-link files can be relocated: ' + str(path))
        if time.time() - info.st_mtime < minimum_age:
            raise RuntimeError('Source day is not closed: ' + str(path))
        records[path.name] = signature(info)
    if not records:
        raise RuntimeError('Empty source day')
    return records


def hash_file(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RuntimeError('Expected a regular file: ' + str(path))
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if signature(os.fstat(stream.fileno())) != signature(before):
            raise RuntimeError('File changed before reading: ' + str(path))
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
        os.fsync(stream.fileno())
        if signature(os.fstat(stream.fileno())) != signature(before):
            raise RuntimeError('File changed while reading: ' + str(path))
    if signature(path.lstat()) != signature(before):
        raise RuntimeError('File changed after reading: ' + str(path))
    return digest.hexdigest()


def attributes(path):
    info = path.lstat()
    return {'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
            'xattrs': {key: os.getxattr(path, key).hex() for key in os.listxattr(path)}}


def mount_roots(plan):
    for name in ('project_mount', 'data_mount'):
        path = safe_path(plan[name])
        if not os.path.ismount(path):
            raise RuntimeError('Required volume is not mounted: ' + str(path))
    if Path(plan['raw_root']).stat().st_dev != Path(plan['project_mount']).stat().st_dev:
        raise RuntimeError('Raw root is on the wrong filesystem')
    parent = Path(plan['destination_root'])
    while not parent.exists():
        parent = parent.parent
    if parent.stat().st_dev != Path(plan['data_mount']).stat().st_dev:
        raise RuntimeError('Relocation destination is on the wrong filesystem')


def free_bytes(path):
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def capacity(plan, remaining_copy=0, reclaimable=0):
    mount_roots(plan)
    project_free = free_bytes(plan['project_mount'])
    data_free = free_bytes(plan['data_mount'])
    if project_free + reclaimable < plan['project_required_free_bytes']:
        raise RuntimeError(f'Project seven-day capacity hold: free={project_free}, potential_reclaim={reclaimable}, required={plan["project_required_free_bytes"]}')
    if data_free < remaining_copy + plan['data_reserve_bytes']:
        raise RuntimeError(f'Data capacity hold: free={data_free}, remaining_copy={remaining_copy}, reserve={plan["data_reserve_bytes"]}')
    return {'project_free': project_free, 'data_free': data_free, 'potential_reclaim': reclaimable}


def inspect(plan):
    mount_roots(plan)
    total, remaining = 0, 0
    for day in plan['days']:
        source, target, quarantine = paths(plan, day)
        if os.path.ismount(source) and not quarantine.exists():
            # A previously released original no longer contributes free-space credit.
            if not source.samefile(target):
                raise RuntimeError('Unexpected mounted source')
            continue
        origin = quarantine if quarantine.exists() else source
        records = snapshot(origin) if not progress_path(plan, day).exists() else original_snapshot(
            plan, day, origin, json.loads(manifest_path(plan, day).read_text()))
        if not progress_path(plan, day).exists() and len(records) != expected_files(plan, day):
            raise RuntimeError('Unexpected source file count: ' + day)
        total += sum((origin / name).stat().st_blocks * 512 for name in records)
        remaining += sum(row[2] for name, row in records.items() if not (target / name).exists())
    return capacity(plan, remaining_copy=remaining, reclaimable=total)


def stage(plan, day):
    """Copy into a separate tree; every source byte and metadata stay in place."""
    inspect(plan)
    source, target, quarantine = paths(plan, day)
    if quarantine.exists() or os.path.ismount(source):
        raise RuntimeError('Day has already entered cutover')
    before = snapshot(source)
    target.mkdir(parents=True, exist_ok=True)
    if set(p.name for p in target.iterdir()) - set(before):
        raise RuntimeError('Unknown or interrupted files in staging; preserve and inspect them')
    evidence = {'day': day, 'source_signature': before, 'destination_signature': {}, 'files': {}}
    for name, expected in before.items():
        src, dst = source / name, target / name
        source_hash = hash_file(src)
        if signature(src.lstat()) != expected:
            raise RuntimeError('Source changed since inventory')
        if not dst.exists():
            temporary = target / ('.copying-' + name)
            # Exclusive creation deliberately retains interrupted copies for inspection.
            with src.open('rb') as inp, temporary.open('xb') as out:
                shutil.copyfileobj(inp, out, 8 * 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.chown(temporary, src.stat().st_uid, src.stat().st_gid)
            shutil.copystat(src, temporary)
            with temporary.open('rb') as out:
                os.fsync(out.fileno())
            os.rename(temporary, dst)
            sync_dir(target)
        destination_before = signature(dst.lstat())
        if (hash_file(dst) != source_hash or attributes(src) != attributes(dst)
                or signature(dst.lstat()) != destination_before):
            raise RuntimeError('Copy differs from source: ' + name)
        evidence['destination_signature'][name] = destination_before
        evidence['files'][name] = {'sha256': source_hash, **attributes(src)}
    if snapshot(source) != before:
        raise RuntimeError('Source day changed during copy')
    os.chown(target, source.stat().st_uid, source.stat().st_gid)
    shutil.copystat(source, target)
    sync_dir(target)
    evidence['verified_at'] = time.time()
    if snapshot(target) != evidence['destination_signature']:
        raise RuntimeError('Destination changed during staging')
    save(manifest_path(plan, day), evidence)
    return {'day': day, 'files': len(before), 'bytes': sum(row[2] for row in before.values())}


def verify_day(plan, day, mounted=False):
    source, target, quarantine = paths(plan, day)
    evidence = json.loads(manifest_path(plan, day).read_text())
    origin = quarantine if mounted else source
    before = original_snapshot(plan, day, origin, evidence)
    destination_before = snapshot(target, minimum_age=0)
    if set(p.name for p in target.iterdir()) != set(evidence['files']):
        raise RuntimeError('Destination file set changed: ' + day)
    for name, record in evidence['files'].items():
        for path in ([origin / name] if name in before else []) + [target / name]:
            if hash_file(path) != record['sha256'] or attributes(path) != {k: v for k, v in record.items() if k != 'sha256'}:
                raise RuntimeError('SHA-256 or metadata mismatch: ' + str(path))
    if original_snapshot(plan, day, origin, evidence) != before:
        raise RuntimeError('Original changed during verification')
    if snapshot(target, minimum_age=0) != destination_before:
        raise RuntimeError('Destination changed during verification')
    evidence['destination_signature'] = destination_before
    evidence['verified_at'] = time.time()
    save(manifest_path(plan, day), evidence)
    return evidence


def verify_fast(plan, day, mounted=False):
    """Use the unchanged identities from full verification for the brief cutover."""
    source, target, quarantine = paths(plan, day)
    evidence = json.loads(manifest_path(plan, day).read_text())
    original_snapshot(plan, day, quarantine if mounted else source, evidence)
    if snapshot(target, minimum_age=0) != evidence['destination_signature']:
        raise RuntimeError('Destination changed since full verification: ' + day)
    if time.time() - evidence['verified_at'] > 6 * 3600:
        raise RuntimeError('Run full verification again before cutover/release')
    return evidence


def guard(plan, additional_bytes=0):
    if additional_bytes < 0:
        raise ValueError('Negative capacity reservation')
    mount_roots(plan)
    for day in plan['days']:
        source, target, _ = paths(plan, day)
        if not os.path.ismount(source) or not source.samefile(target):
            raise RuntimeError('Relocated raw day is not mounted from its verified destination: ' + day)
        if source.stat().st_dev != Path(plan['data_mount']).stat().st_dev:
            raise RuntimeError('Relocated day is on the wrong filesystem')
        options = subprocess.check_output(['findmnt', '--noheadings', '--output', 'OPTIONS', '--mountpoint', str(source)], text=True).strip().split(',')
        if 'ro' not in options:
            raise RuntimeError('Closed relocated day must remain read-only')
        # systemd can create a missing bind source directory. Mount identity alone
        # would then accept an empty view. Check the already-hashed file identities
        # without reading observation bytes on every consumer start.
        evidence = json.loads(manifest_path(plan, day).read_text())
        expected = evidence['destination_signature']
        if (evidence['day'] != day or len(expected) != expected_files(plan, day)
                or set(expected) != set(evidence['files'])
                or snapshot(target, minimum_age=0) != expected):
            raise RuntimeError('Relocated destination differs from verified manifest: ' + day)
    if free_bytes(plan['data_mount']) < plan['data_reserve_bytes'] + additional_bytes:
        raise RuntimeError('Relocated raw data reserve is unavailable')


def unit_name(path):
    return subprocess.check_output(['systemd-escape', '--path', '--suffix=mount', str(path)], text=True).strip()


def unit_text(source, target):
    return ('[Unit]\nDescription=Preserved closed Aurora radar day\n'
            'Requires=project.mount data.mount\nAfter=project.mount data.mount\n'
            'BindsTo=project.mount data.mount\n'
            f'AssertPathIsDirectory={target}\n\n[Mount]\n'
            f'What={target}\nWhere={source}\nType=none\nOptions=bind,ro\n'
            'TimeoutSec=30\n\n[Install]\nWantedBy=multi-user.target\n')


def dropin_text(plan):
    mounts = ' '.join(unit_name(paths(plan, day)[0]) for day in plan['days'])
    return (f'[Unit]\nRequires={mounts}\nAfter={mounts}\nBindsTo={mounts}\n\n'
            f'[Service]\nExecStartPre={HELPER} guard --config {ACTIVE}\n')


def launch_hold_text(plan):
    return ('[Unit]\n# Block new raw readers while a reviewed relocation is in progress.\n'
            f'ConditionPathExists=!{plan["launch_hold"]}\n')


def assert_paused(plan):
    # Timer inactivity alone does not cover path/ExecStopPost/queue launches.
    # Older staging plans remain readable, but cannot cut over without this gate.
    hold = safe_path(plan.get('launch_hold', '/missing-relocation-launch-hold'))
    if not hold.is_file() or not plan.get('launchers'):
        raise RuntimeError('Activate the reviewed raw-reader launch barrier before cutover')
    for service in plan['consumers']:
        dropin = SYSTEMD_ROOT / (service + '.d') / '50-radar-relocation-hold.conf'
        if not dropin.is_file() or dropin.read_text() != launch_hold_text(plan):
            raise RuntimeError('Missing or changed raw-reader launch barrier: ' + service)
    for service in plan['consumers'] + plan['launchers']:
        for unit in (service, service.replace('.service', '.timer')):
            state = subprocess.check_output(['systemctl', 'show', unit, '--property=LoadState,ActiveState,MainPID,Job,NeedDaemonReload'], text=True)
            fields = dict(line.split('=', 1) for line in state.splitlines())
            if fields.get('LoadState') == 'not-found':
                if unit.endswith('.timer'):
                    continue
                raise RuntimeError('Required consumer is unknown: ' + unit)
            if fields.get('NeedDaemonReload') != 'no':
                raise RuntimeError('Reload the reviewed raw-reader launch barrier: ' + unit)
            if fields.get('ActiveState') not in ('inactive', 'failed') or fields.get('MainPID', '0') != '0' or fields.get('Job', '') not in ('', '0'):
                raise RuntimeError('Pause and drain raw consumer before cutover: ' + unit)


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    sync_dir(path.parent)


def cutover(plan):
    """Keep originals in quarantine; an interrupted cutover stays fail closed."""
    assert_paused(plan)
    inspect(plan)
    for day in plan['days']:
        verify_fast(plan, day)
        source, _, quarantine = paths(plan, day)
        if quarantine.exists():
            raise RuntimeError('Quarantine already exists')
        if (SYSTEMD_ROOT / unit_name(source)).exists():
            raise RuntimeError('Mount unit already exists; inspect before proceeding')
    for service in plan['consumers']:
        if (SYSTEMD_ROOT / (service + '.d') / '60-radar-relocation.conf').exists():
            raise RuntimeError('Consumer drop-in already exists; inspect before proceeding')
    # The active record exists before the first rename, so guarded remote readers stop.
    if ACTIVE.exists():
        raise RuntimeError('Relocation already active; inspect or roll back')
    save(ACTIVE, plan)
    for day in plan['days']:
        source, target, quarantine = paths(plan, day)
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, quarantine)
        sync_dir(quarantine.parent)
        source.mkdir(mode=0o755)
        sync_dir(source.parent)
        write_text(SYSTEMD_ROOT / unit_name(source), unit_text(source, target))
    for service in plan['consumers']:
        write_text(SYSTEMD_ROOT / (service + '.d') / '60-radar-relocation.conf', dropin_text(plan))
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    for day in plan['days']:
        subprocess.run(['systemctl', 'enable', '--now', unit_name(paths(plan, day)[0])], check=True)
    guard(plan)
    return {'mounted': plan['days'], 'originals': 'preserved in quarantine', 'timers': 'remain paused'}


def graph(plan):
    mounts = {unit_name(paths(plan, day)[0]) for day in plan['days']}
    for service in plan['consumers']:
        state = subprocess.check_output(['systemctl', 'show', service, '--property=Requires,After,BindsTo,ExecStartPre'], text=True)
        fields = dict(line.split('=', 1) for line in state.splitlines())
        # systemctl quotes and escapes unit names containing literal hyphens in
        # their source paths. Parse one display-escaping layer before comparison.
        if any(not mounts.issubset(set(shlex.split(fields.get(prop, '')))) for prop in ('Requires', 'After', 'BindsTo')) or HELPER not in fields.get('ExecStartPre', ''):
            raise RuntimeError('Consumer mount dependencies/guard are incomplete: ' + service)
    return {'consumers': len(plan['consumers']), 'mounts': len(mounts)}


def release(plan, day):
    """Remove only a verified duplicate original, never the logical observation."""
    assert_paused(plan)
    guard(plan)
    graph(plan)
    inspect(plan)
    evidence = verify_fast(plan, day, mounted=True)
    source, target, quarantine = paths(plan, day)
    # Durable evidence is written before removing any duplicate original bytes.
    progress_file = progress_path(plan, day)
    progress = json.loads(progress_file.read_text()) if progress_file.exists() else {
        'time': time.time(), 'logical': str(source), 'destination': str(target),
        'evidence': evidence, 'released': [], 'pending': None}
    save(progress_file, progress)
    for name, expected in evidence['source_signature'].items():
        original = quarantine / name
        guard(plan)
        if signature((target / name).lstat()) != evidence['destination_signature'][name]:
            raise RuntimeError('File changed before releasing duplicate original')
        if not original.exists():
            if name not in progress['released'] and name != progress['pending']:
                raise RuntimeError('Original missing without a durable release intent')
        else:
            if signature(original.lstat()) != expected:
                raise RuntimeError('Original changed before releasing duplicate')
            progress['pending'] = name
            save(progress_file, progress)
            original.unlink()
            sync_dir(quarantine)
        if name not in progress['released']: progress['released'].append(name)
        progress['pending'] = None
        save(progress_file, progress)
    if quarantine.exists():
        quarantine.rmdir()
        sync_dir(quarantine.parent)
    return {'released_duplicate_day': day, 'logical_path': str(source)}


def rollback(plan):
    """Restore quarantined originals, including an interrupted partial cutover."""
    assert_paused(plan)
    # Refuse rollback after even one original has been released.
    for day in plan['days']:
        source, _, quarantine = paths(plan, day)
        progress = progress_path(plan, day)
        if progress.exists():
            state = json.loads(progress.read_text())
            if state.get('released') or state.get('pending'):
                raise RuntimeError('Original release has started; rollback requires a verified recopy')
        if not quarantine.exists() and os.path.ismount(source):
            raise RuntimeError('Original already released; rollback requires a verified recopy')
        verify_day(plan, day, mounted=quarantine.exists())
    for service in plan['consumers']:
        dropin = SYSTEMD_ROOT / (service + '.d') / '60-radar-relocation.conf'
        if dropin.exists():
            if dropin.read_text() != dropin_text(plan):
                raise RuntimeError('Consumer drop-in changed; preserve and inspect it')
            dropin.unlink()
            sync_dir(dropin.parent)
    for day in plan['days']:
        source, target, quarantine = paths(plan, day)
        unit = SYSTEMD_ROOT / unit_name(source)
        if unit.exists():
            if unit.read_text() != unit_text(source, target):
                raise RuntimeError('Mount definition changed; preserve and inspect it')
            subprocess.run(['systemctl', 'disable', '--now', unit.name], check=True)
            unit.unlink()
            sync_dir(unit.parent)
        if quarantine.exists():
            # rmdir refuses unexpected data; never recursively remove the logical path.
            if source.exists(): source.rmdir()
            os.rename(quarantine, source)
            sync_dir(source.parent)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    ACTIVE.unlink()
    sync_dir(ACTIVE.parent)
    return {'restored': plan['days'], 'destination_copies': 'preserved', 'timers': 'remain paused'}


@contextmanager
def lock(exclusive):
    # Installed root-owned, aurora-readable lock; held by the sender supervisor.
    with LOCK.open('r') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def run_sender(command):
    """Keep our lock while sudo/rsync runs, even if it closes inherited FDs."""
    child = None
    pending = []
    denied_signal = None

    def forward(signum, _frame):
        nonlocal denied_signal
        if child is None:
            pending.append(signum)
        else:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass  # The child exited before the pending signal was delivered.
            except PermissionError:
                # A setuid sender may no longer be signalable by this user.
                # Keep supervising it; never unlock while it can still read raw.
                denied_signal = denied_signal or signum

    watched = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous = {sig: signal.signal(sig, forward) for sig in watched}
    try:
        # A separate process group lets us forward termination to sudo and its
        # sender together. The parent waits for completion before releasing lock.
        child = subprocess.Popen(command, start_new_session=True)
        for signum in pending:
            forward(signum, None)
        status = child.wait()
        if denied_signal is not None:
            return 128 + denied_signal
        return status if status >= 0 else 128 - status
    finally:
        # An unexpected local error must not release the lock over a live sender.
        if child is not None and child.poll() is None:
            child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'stage', 'verify', 'cutover', 'guard', 'graph', 'release', 'rollback', 'exec'))
    parser.add_argument('--config', type=Path, default=ACTIVE)
    parser.add_argument('--day')
    parser.add_argument('--if-active', action='store_true')
    parser.add_argument('--additional-bytes', type=int, default=0)
    args, command = parser.parse_known_args()
    if command and args.action != 'exec': parser.error('Unexpected trailing arguments')
    try:
        with lock(args.action in ('cutover', 'release', 'rollback')):
            if args.if_active and not args.config.exists() and args.action in ('guard', 'exec'):
                result = {'relocation': 'not active'}
            else:
                plan = load(args.config)
                if args.action == 'inspect': result = inspect(plan)
                elif args.action == 'stage': result = stage(plan, args.day)
                elif args.action == 'verify':
                    result = {}
                    for day in ([args.day] if args.day else plan['days']):
                        evidence = verify_day(plan, day, mounted=os.path.ismount(paths(plan, day)[0]))
                        result[day] = {'files': len(evidence['files']), 'verified_at': evidence['verified_at']}
                elif args.action == 'cutover': result = cutover(plan)
                elif args.action == 'graph': result = graph(plan)
                elif args.action == 'release': result = release(plan, args.day)
                elif args.action == 'rollback': result = rollback(plan)
                else:
                    guard(plan, args.additional_bytes)
                    result = {'relocation': 'mounted'}
            if args.action == 'exec':
                command = command[1:] if command[:1] == ['--'] else command
                if not command: raise ValueError('Missing sender command')
                return run_sender(command)
            print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 75


if __name__ == '__main__':
    raise SystemExit(main())
