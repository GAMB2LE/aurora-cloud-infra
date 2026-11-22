"""Journal a bounded radar maintenance window without terminating active payloads."""
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import sys
import time
import uuid

BASE = Path('/var/lib/aurora-cloud/radar-retention-windows')
ASS_LOCK = Path('/var/lib/aurora-cloud/retention-windows/window.lock')
HOLD = Path('/etc/aurora/radar-relocation.hold')
COORD = Path('/var/lib/aurora-radar-coordinator-v1/output/aurora-archive-repair-20261026/archive-first-implementation-20261027')
RELEASE = Path('/var/lib/aurora-radar-coordinator-v1/native-backfill-project-override-v3')
PLAN_SHA = 'e9534ca975fd24517bc0db56fcb9a5bb268c2e16b5d07ce29a5b9fa6621d8bcb'
PLAN = Path('/etc/aurora/radar-relocation-plan.json')


class AdmissionDeferred(TimeoutError):
    """A pre-mutation admission wait ended with protection safely restored."""


def tools():
    root = Path('/usr/local/lib/aurora-retention-window')
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location('retention_window_tools', root / 'aurora-ass-retention-window.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Maintenance:
    def __init__(self, purpose, *, folder=None, utility=None):
        if not re.fullmatch('[a-z0-9-]{3,80}', purpose):
            raise ValueError('invalid maintenance purpose')
        self.t = utility or tools()
        self.folder = folder
        self.purpose = purpose
        self.fds = []
        self.state = None

    def save(self):
        self.t.save(self.folder / 'state.json', self.state)

    def unsafe(self):
        self.state['safe_to_resume'] = False
        self.save()

    def safe(self):
        self.state['safe_to_resume'] = True
        self.save()

    def fail_admission(self, error):
        self.state['admission_error'] = type(error).__name__ + ': ' + str(error)
        self.save()
        # Only the entry path calls this: no mount replacement or unlink has
        # been authorized. A failed restore must remain an ordinary hard error.
        self.restore()
        if isinstance(error, (TimeoutError, BlockingIOError)):
            raise AdmissionDeferred(str(error)) from error
        raise error

    def pause_timer(self, unit, *, field=False):
        if self.t.props(unit, field)['ActiveState'] == 'active':
            key = 'resume_field_units' if field else 'resume_cloud_units'
            if unit not in self.state[key]:
                self.state[key].append(unit)
                self.save()
            command = ['systemctl', 'stop', unit]
            self.t.run([*self.t.FIELD, shlex.join(command)] if field else command)

    def close_locks(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()

    def pause_plan_timers(self):
        if self.t.digest(PLAN) != PLAN_SHA:
            raise ValueError('reviewed relocation plan differs')
        plan = json.loads(self.t.control(PLAN))
        self.state['consumers'] = plan['consumers']; self.save()
        for unit in dict.fromkeys(plan['consumers'] + plan.get('launchers', [])):
            timer = unit.removesuffix('.service') + '.timer'
            loaded = self.t.run(['systemctl', 'show', timer, '-p', 'LoadState', '--value']).strip()
            if loaded != 'not-found':
                self.pause_timer(timer)
        return plan

    def reenter_exclusive(self):
        if HOLD.exists():
            if self.t.control(HOLD).decode() != self.state['barrier_content']:
                raise ValueError('another operator changed the barrier; do not overwrite it')
        else:
            with HOLD.open('x') as stream:
                stream.write(self.state['barrier_content']); stream.flush(); os.fsync(stream.fileno())
            self.t.syncdir(HOLD.parent)
        deadline = time.monotonic() + 600
        while True:
            try:
                fcntl.flock(self.relocation_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('a live payload still owns the shared lease; rollback remains held')
                time.sleep(3)

    def handoff_check(self, check):
        """Prove normal admission under a shared lease before resuming any timers."""
        if self.t.control(HOLD).decode() != self.state['barrier_content']:
            raise ValueError('owned barrier differs before admission check')
        HOLD.unlink(); self.t.syncdir(HOLD.parent)
        fcntl.flock(self.relocation_fd, fcntl.LOCK_SH)
        self.state['handoff_check_started_at'] = dt.datetime.now(dt.timezone.utc).isoformat(); self.save()
        try:
            result = check()
        except BaseException:
            self.reenter_exclusive()
            raise
        self.state['handoff_check_passed_at'] = dt.datetime.now(dt.timezone.utc).isoformat(); self.save()
        return result

    def restore(self):
        if self.state is None or self.state.get('restored_at'):
            self.close_locks()
            return
        if not self.state.get('safe_to_resume'):
            self.close_locks()
            raise RuntimeError('maintenance mutation is unverified; owned barrier and paused admissions remain')
        if self.state.get('barrier_created') and HOLD.exists():
            if self.t.control(HOLD).decode() != self.state['barrier_content']:
                raise ValueError('maintenance barrier changed; refuse to remove it')
            HOLD.unlink(); self.t.syncdir(HOLD.parent)
        # Release locks before resuming timers: a missed persistent timer can fire
        # immediately and must be able to acquire the same admission locks.
        self.close_locks()
        for unit in self.state['resume_cloud_units']:
            self.t.run(['systemctl', 'start', unit], timeout=90)
        for unit in self.state['resume_field_units']:
            self.t.run([*self.t.FIELD, shlex.join(['systemctl', 'start', unit])], timeout=90)
        self.state['restored_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.save()

    def __enter__(self):
        BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
        if BASE.resolve() != BASE or BASE.stat().st_uid or BASE.stat().st_mode & 0o077:
            raise ValueError('unsafe maintenance state directory')
        current = BASE / 'current.json'
        self.fds.append(self.t.take_lock(ASS_LOCK, 0))
        if current.exists():
            previous = json.loads(self.t.control(current))['run']
            if Path(previous).name != previous:
                raise ValueError('unsafe previous maintenance pointer')
            old = json.loads(self.t.control(BASE / previous / 'state.json'))
            if not old.get('restored_at'):
                self.close_locks()
                raise ValueError('previous maintenance needs reconciliation before another window')
        if self.t.digest(self.t.HOLD) != self.t.HOLD_SHA or os.path.lexists(HOLD):
            self.close_locks()
            raise ValueError('legacy protection differs or another radar barrier exists')
        self.folder = BASE / (self.purpose + '-' + uuid.uuid4().hex)
        self.folder.mkdir(mode=0o700)
        self.state = {'schema': 'radar-retention-maintenance-v1', 'purpose': self.purpose,
                      'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                      'safe_to_resume': True, 'resume_cloud_units': [], 'resume_field_units': []}
        self.save(); self.t.save(current, {'run': self.folder.name})
        try:
            # Stop every reviewed admission schedule before waiting for any one
            # payload. Otherwise a fresh long transfer can start during the
            # mirror drain and repeatedly prevent this maintenance window.
            plan = self.pause_plan_timers()
            self.pause_timer('aurora-mirror-verify.timer')
            self.t.wait_inactive('aurora-mirror-verify.service', 1800)
            self.pause_timer('aurora-native-fresh-discovery.timer', field=True)
            self.t.wait_inactive('aurora-native-fresh-discovery.service', 180, True)
            for name in ('per-file-coordinator.lock', 'bounded-backlog-coordinator.lock'):
                self.fds.append(self.t.take_lock(COORD / name, pwd.getpwnam('aurora').pw_uid))
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
            settled = self.t.props(unit)
            if settled['ActiveState'] == 'active':
                pid = settled['MainPID']; proc = Path('/proc') / pid
                if (not pid.isdigit() or int(pid) <= 0
                        or (proc / 'task' / pid / 'children').read_text().split()
                        or set(p.name for p in (proc / 'fd').iterdir()) != {'0', '1', '2'}
                        or b'settled_overnight_with_supplement.py' not in (proc / 'cmdline').read_bytes()):
                    raise BlockingIOError('settled worker is not proven idle; leave it running')
                self.state['resume_cloud_units'].insert(0, unit); self.save()
                self.t.run(['systemctl', 'stop', unit])
            elif settled['ActiveState'] not in ('inactive', 'failed'):
                raise BlockingIOError('settled worker is changing state')
            content = 'Owned radar retention maintenance: ' + self.folder.name + '\n'
            self.state.update(barrier_content=content, barrier_created=True); self.save()
            with HOLD.open('x') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            self.t.syncdir(HOLD.parent)
            if self.t.digest(PLAN) != PLAN_SHA:
                raise ValueError('reviewed relocation plan differs')
            for consumer in plan['consumers']:
                loaded = self.t.run(['systemctl', 'show', consumer, '-p', 'LoadState', '--value']).strip()
                if loaded != 'not-found':
                    self.t.wait_inactive(consumer, 1800)
            self.relocation_fd = self.t.take_lock(Path('/run/lock/aurora-radar-relocation.lock'), 0, timeout=1200)
            self.fds.append(self.relocation_fd)
            if self.t.digest(self.t.HOLD) != self.t.HOLD_SHA:
                raise ValueError('legacy protection changed during drain')
            self.state['drained_at'] = dt.datetime.now(dt.timezone.utc).isoformat(); self.save()
            return self
        except BaseException as error:
            self.fail_admission(error)

    def __exit__(self, exc_type, exc, traceback):
        if exc is not None:
            self.state['error_type'] = type(exc).__name__; self.save()
        self.restore()
        return False


def restore_current():
    utility = tools()
    pointer = BASE / 'current.json'
    if not pointer.exists():
        return
    name = json.loads(utility.control(pointer))['run']
    if Path(name).name != name:
        raise ValueError('unsafe maintenance pointer')
    instance = Maintenance('restore', folder=BASE / name, utility=utility)
    instance.state = json.loads(utility.control(instance.folder / 'state.json'))
    instance.restore()
