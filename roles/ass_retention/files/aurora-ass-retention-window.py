#!/usr/bin/env python3
"""Run signed retention inside a drained, audited window; restore the legacy hold."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import runpy
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from retention_window_gate import require_ready, require_receipts, require_absent

BASE = Path('/var/lib/aurora-cloud/retention-windows')
HOLD = Path('/etc/aurora/ass-retention.hold')
HOLD_SHA = '22dce428efe03586a4cdf751c327a9c34e685922a9907c8b193d5d684cab3e24'
DROP = Path('/run/systemd/system/aurora-ass-retention.service.d/99-retention-window.conf')
COORD = Path('/var/lib/aurora-radar-coordinator-v1/output/aurora-archive-repair-20261026/archive-first-implementation-20261027')
RELEASE = Path('/var/lib/aurora-radar-coordinator-v1/native-backfill-project-override-v3')
FIELD = ['sudo', '-n', '-u', 'aurora', 'ssh', '-oBatchMode=yes',
         '-oConnectTimeout=15', 'root@100.124.55.22']
SIGNED = 'aurora-ass-retention.service'


class GateDeferred(ValueError):
    """Fresh archive confirmation is required before any deletion release."""


def run(args, timeout=60, input_data=None):
    return subprocess.run(args, input=input_data, capture_output=True, text=True,
                          check=True, timeout=timeout).stdout


def props(unit, field=False):
    command = ['systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'MainPID',
               '-p', 'ControlPID', '-p', 'Result', '-p', 'InvocationID', '-p', 'ConditionResult', '-p', 'ExecMainStatus']
    text = run([*FIELD, shlex.join(command)] if field else command)
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def syncdir(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.state-')
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    syncdir(path.parent)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def control(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid or info.st_nlink != 1
            or info.st_mode & 0o022 or path.resolve() != path):
        raise ValueError('unsafe control file: ' + str(path))
    return path.read_bytes()


def wait_inactive(unit, timeout, field=False):
    deadline = time.monotonic() + timeout
    while True:
        current = props(unit, field)
        if current['ActiveState'] in ('inactive', 'failed') and current['MainPID'] == '0':
            return current
        if time.monotonic() >= deadline:
            raise TimeoutError('payload still running; no restart attempted: ' + unit)
        time.sleep(3)


def take_lock(path, owner, timeout=120):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (path.stat().st_dev, path.stat().st_ino)):
        os.close(fd)
        raise ValueError('lock identity differs: ' + str(path))
    previous = signal.getsignal(signal.SIGALRM)
    def expired(signum, frame):
        raise TimeoutError('active work still owns lock: ' + str(path))
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(max(1, int(timeout)))
    try:
        # Queue in the kernel rather than repeatedly racing readers between
        # consecutive files. Existing payloads keep their leases until finished.
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('lock path changed while waiting: ' + str(path))
        return fd
    except BaseException:
        os.close(fd)
        raise
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def current_folder():
    pointer = BASE / 'current.json'
    if not pointer.exists():
        return None
    name = json.loads(control(pointer))['run']
    if Path(name).name != name or not name.startswith('window-'):
        raise ValueError('invalid current window pointer')
    return BASE / name


def restore(folder=None, resume=True):
    folder = folder or current_folder()
    if folder is None:
        return
    state_path = folder / 'state.json'
    state = json.loads(control(state_path))
    if state.get('restored_at'):
        return
    backup = folder / 'hold.original'
    raw = control(backup)
    if hashlib.sha256(raw).hexdigest() != HOLD_SHA:
        raise ValueError('original hold backup differs')
    if HOLD.exists():
        if hashlib.sha256(control(HOLD)).hexdigest() != HOLD_SHA:
            raise ValueError('hold changed; refuse automatic overwrite')
    else:
        fd, temporary = tempfile.mkstemp(dir=HOLD.parent, prefix='.retention-restore-')
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); os.fchmod(stream.fileno(), state['hold_mode'])
            os.fchown(stream.fileno(), state['hold_uid'], state['hold_gid'])
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, HOLD); syncdir(HOLD.parent)
    if DROP.exists():
        if control(DROP).decode() != state.get('override'):
            raise ValueError('signed worker override changed')
        DROP.unlink(); syncdir(DROP.parent)
        run(['systemctl', 'daemon-reload'])
    if not resume:
        return
    # Never resume admissions while a separately started signed worker is active.
    signed = props(SIGNED)
    # At boot the signed unit can be queued behind this restore service. A
    # queued start has no payload and will see the restored hold when it starts.
    if not (signed['ActiveState'] == 'activating' and signed['MainPID'] == '0'
            and signed.get('ControlPID', '0') == '0'):
        wait_inactive(SIGNED, 600)
    for unit in state['resume_cloud_units']:
        run(['systemctl', 'start', unit], timeout=90)
    for unit in state['resume_field_units']:
        run([*FIELD, shlex.join(['systemctl', 'start', unit])], timeout=90)
    state['restored_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    save(state_path, state)


def mirror_gate(module):
    summary = json.loads((module['MIRROR'] / 'summary.json').read_text())
    gate = json.loads(module['OBJECT_GATE'].read_text())
    names = [s['name'] for s in module['STREAMS'] if s.get('prune_enabled')]
    try:
        require_ready(gate, summary, names, dt.datetime.now(dt.timezone.utc))
    except ValueError as error:
        raise GateDeferred(str(error)) from error
    return summary


def field_metadata(rows):
    code = '''import json,os,stat,sys
result={}
for name in json.loads(sys.argv[1]):
 try:
  s=os.lstat(name)
  if not stat.S_ISREG(s.st_mode): raise ValueError('unexpected source type')
  result[name]=[s.st_size,s.st_mtime_ns//10**9]
 except FileNotFoundError: result[name]=None
print(json.dumps(result))'''
    result = {}
    for start in range(0, len(rows), 100):
        result.update(json.loads(run([*FIELD, shlex.join(['python3', '-c', code,
            json.dumps([r['path'] for r in rows[start:start + 100]])])])))
    return result


def field_audit(receipts):
    code = '''import json,sys
from pathlib import Path
expected=json.load(sys.stdin);keys=set(expected)
config=json.loads(Path('/etc/aurora-edge/retention.json').read_text())
found={}
with Path(config['audit_log']).open() as stream:
 for line in stream:
  row=json.loads(line)
  if row.get('permit_sha256') in keys:
   if row['permit_sha256'] in found: raise ValueError('duplicate edge audit receipt')
   found[row['permit_sha256']]=row
if found!=expected: raise ValueError('independent edge audit differs from cloud receipts')
s=__import__('os').statvfs('/home/aurora/data')
print(json.dumps({'edge_receipts_match':True,'available_bytes':s.f_bavail*s.f_frsize}))'''
    expected = {row['permit_sha256']: row for row in receipts}
    if len(expected) != len(receipts):
        raise ValueError('duplicate signed permit receipt')
    return json.loads(run([*FIELD, shlex.join(['python3', '-c', code])], input_data=json.dumps(expected)))


def perform(maximum):
    if not 1 <= maximum <= 5000:
        raise ValueError('window limit must be between 1 and 5000')
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if BASE.resolve() != BASE or BASE.stat().st_uid or BASE.stat().st_mode & 0o077:
        raise ValueError('unsafe window state directory')
    with (BASE / 'window.lock').open('a+') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = current_folder()
        if previous:
            old = json.loads(control(previous / 'state.json'))
            if not old.get('restored_at'):
                raise ValueError('previous window restoration is incomplete')
            if old.get('hold_release_at') and not old.get('receipt_audit_complete'):
                raise ValueError('previous signed batch requires independent reconciliation')
        if DROP.exists():
            raise ValueError('unexpected signed worker override')
        original = control(HOLD)
        if hashlib.sha256(original).hexdigest() != HOLD_SHA:
            raise ValueError('legacy radar-protection hold differs')
        module = runpy.run_path('/usr/local/bin/aurora-ass-retention', run_name='window_gate')
        mirror_gate(module)  # Reject stale proof before changing any timer or admission.
        run(['/usr/local/lib/aurora-asfs-storage-guard.py', 'check'])
        folder = BASE / ('window-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
        folder.mkdir(mode=0o700)
        info = HOLD.stat()
        with (folder / 'hold.original').open('xb') as backup:
            backup.write(original); backup.flush(); os.fsync(backup.fileno())
        state = {'schema': 'aurora-scheduled-retention-window-v1',
                 'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                 'hold_uid': info.st_uid, 'hold_gid': info.st_gid,
                 'hold_mode': stat.S_IMODE(info.st_mode), 'max_candidates': maximum,
                 'resume_cloud_units': [], 'resume_field_units': [],
                 'receipt_audit_complete': False}
        save(folder / 'state.json', state)
        save(BASE / 'current.json', {'run': folder.name})
        locks = []
        try:
            if props('aurora-mirror-verify.timer')['ActiveState'] == 'active':
                state['resume_cloud_units'].append('aurora-mirror-verify.timer'); save(folder / 'state.json', state)
            run(['systemctl', 'stop', 'aurora-mirror-verify.timer'])
            wait_inactive('aurora-mirror-verify.service', 1200)
            field_timer = 'aurora-native-fresh-discovery.timer'
            if props(field_timer, True)['ActiveState'] == 'active':
                state['resume_field_units'].append(field_timer); save(folder / 'state.json', state)
            run([*FIELD, shlex.join(['systemctl', 'stop', field_timer])])
            wait_inactive('aurora-native-fresh-discovery.service', 180, True)
            for name in ('per-file-coordinator.lock', 'bounded-backlog-coordinator.lock'):
                locks.append(take_lock(COORD / name, pwd.getpwnam('aurora').pw_uid))
            sys.path.insert(0, str(RELEASE))
            from per_file_queue import Queue
            for name in ('fresh-finalized-queue.sqlite', 'per-file-archive-queue.sqlite'):
                queue = Queue(COORD / name, readonly=True)
                try:
                    if queue.outstanding() is not None:
                        raise BlockingIOError('archive operation still requires reconciliation')
                finally:
                    queue.close()
            unit = 'aurora-native-settled-backfill.service'
            settled = props(unit)
            if settled['ActiveState'] == 'active':
                pid = settled['MainPID']; proc = Path('/proc') / pid
                if (not pid.isdigit() or int(pid) <= 0
                        or (proc / 'task' / pid / 'children').read_text().split()
                        or set(p.name for p in (proc / 'fd').iterdir()) != {'0', '1', '2'}
                        or b'settled_overnight_with_supplement.py' not in (proc / 'cmdline').read_bytes()):
                    raise BlockingIOError('settled worker is not proven idle; leave it running')
                state['resume_cloud_units'].insert(0, unit); save(folder / 'state.json', state)
                run(['systemctl', 'stop', unit])
            elif settled['ActiveState'] not in ('inactive', 'failed'):
                raise BlockingIOError('settled worker is changing state')
            locks.append(take_lock(Path('/run/lock/aurora-radar-relocation.lock'), 0, timeout=1200))
            import retention_candidate_readback as readback
            snapshot, candidates = readback.select(maximum)
            # Every path remains governed by strict manifests and signed exact-path
            # permits. Independently read full bytes from all four copies for the sample.
            sample = candidates[:min(5, len(candidates))]
            readback.verify(folder / 'sample-readback.json', sample, snapshot)
            summary = mirror_gate(module)
            latest = module['MIRROR'].parent / 'history' / module['timestamp'](summary['generated_at']).strftime('%Y%m%dT%H%M%SZ')
            if latest != snapshot:
                raise ValueError('candidate manifest changed during readback')
            metadata = field_metadata(candidates)
            if any(metadata.get(r['path']) != [r['size'], r['mtime']] for r in candidates):
                raise ValueError('selected source versions changed before release')
            signed = props(SIGNED)
            if signed['ActiveState'] not in ('inactive', 'failed') or signed['MainPID'] != '0':
                raise BlockingIOError('another signed retention run is active')
            before = set(module['AUDIT'].glob('*.json'))
            state.update(candidates=candidates, mirror_snapshot=str(snapshot),
                         prior_invocation=signed['InvocationID'],
                         override='[Service]\nExecStart=\nExecStart=/usr/local/bin/aurora-ass-retention --max-candidates ' + str(maximum) + '\n')
            state['edge_available_bytes_before'] = field_audit([])['available_bytes']
            save(folder / 'state.json', state)
            DROP.parent.mkdir(parents=True, exist_ok=True)
            with DROP.open('x') as out:
                out.write(state['override']); out.flush(); os.fsync(out.fileno())
            run(['systemctl', 'daemon-reload'])
            if control(HOLD) != original:
                raise ValueError('hold changed before release')
            mirror_gate(module)
            state['hold_release_at'] = dt.datetime.now(dt.timezone.utc).isoformat(); save(folder / 'state.json', state)
            HOLD.unlink(); syncdir(HOLD.parent)
            run(['systemctl', 'reset-failed', SIGNED])
            run(['systemctl', 'start', '--no-block', SIGNED])
            deadline = time.monotonic() + 600
            while True:
                result = props(SIGNED)
                if (result['InvocationID'] != signed['InvocationID'] and result['InvocationID']
                        and result['ActiveState'] in ('inactive', 'failed') and result['MainPID'] == '0'):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('signed worker still running; restoration must wait')
                time.sleep(2)
            state['signed_result'] = result
            paths = sorted(set(module['AUDIT'].glob('*.json')) - before)
            state['receipts'] = [str(p) for p in paths]; save(folder / 'state.json', state)
            if result['Result'] != 'success' or result['ExecMainStatus'] != '0' or result['ConditionResult'] != 'yes':
                raise ValueError('signed worker failed or was skipped')
            require_receipts([json.loads(p.read_text()) for p in paths], candidates)
            state['independent_edge_audit'] = field_audit([json.loads(p.read_text()) for p in paths])
            after = field_metadata(candidates)
            state['source_paths_absent'] = sum(after.get(r['path']) is None for r in candidates)
            require_absent(after, candidates)
            state['source_absence_verified'] = True
            # Re-read the independently verified sample's surviving primary and archives.
            readback.verify_survivors(folder / 'sample-readback.json', folder / 'post-readback.json')
            state['receipt_audit_complete'] = True
            state['completed_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            save(folder / 'state.json', state)
            print(json.dumps({'audited_signed_retention_window': str(folder),
                              'candidate_count': len(candidates), 'source_paths_absent': state['source_paths_absent']}), flush=True)
        except BaseException as error:
            state['error_type'] = type(error).__name__
            save(folder / 'state.json', state)
            raise
        finally:
            try:
                restore(folder, resume=False)
            finally:
                for fd in reversed(locks):
                    os.close(fd)
            restore(folder)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'restore'))
    parser.add_argument('--max-candidates', type=int, default=500)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('root window control required')
    if args.action == 'restore':
        restore()
    else:
        try:
            perform(args.max_candidates)
        except GateDeferred as error:
            print(json.dumps({'retention_deferred': str(error), 'deletion_release_authorized': False}), file=sys.stderr)
            raise SystemExit(75)
