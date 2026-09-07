#!/usr/bin/env python3
"""Focused two-file deployment; never restarts a service or edits runtime state."""

import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile


WRAPPER = Path('/usr/local/bin/aurora-hatpro-sync')
DRIVER = Path('/usr/local/lib/aurora-hatpro-discovery.py')
BACKUPS = Path('/var/lib/aurora-cloud/recovery-rollbacks')
UNITS = ('aurora-hatpro-source-sync.service', 'aurora-hatpro-backfill.service')
PROTECTED = tuple(Path(p) for p in (
    '/usr/local/bin/aurora-hatpro-backfill',
    '/usr/local/bin/aurora-archive-dispatch',
    '/etc/aurora-object-store/catalog.json',
    '/etc/systemd/system/aurora-hatpro-source-sync.service',
    '/etc/systemd/system/aurora-hatpro-source-sync.timer',
    '/etc/systemd/system/aurora-hatpro-backfill.service',
    '/etc/systemd/system/aurora-hatpro-backfill.timer',
))
KEYS = ('source_user', 'source_host', 'source_port', 'source_path', 'source_pattern',
        'destination', 'state_file', 'source_auth', 'ssh_key', 'known_hosts', 'start_fresh')


def configuration(source):
    values = {}
    for line in source.splitlines():
        key, separator, value = line.partition('=')
        if separator and key in KEYS:
            parts = shlex.split(value)
            if len(parts) != 1:
                raise ValueError('Unsupported source assignment: ' + key)
            values[key] = parts[0]
    if not values:
        # POSIX shells remove escaped newlines before word splitting; shlex
        # alone retains them as tokens and is not a complete shell parser.
        tokens = shlex.split(source.replace('\\\n', ''), comments=True)
        begin = tokens.index(str(DRIVER)) + 1
        tokens = tokens[begin:]
        values['start_fresh'] = '0'
        while tokens:
            flag = tokens.pop(0)
            if flag == '--start-fresh':
                values['start_fresh'] = '1'
                continue
            key = flag.removeprefix('--').replace('-', '_')
            if key not in KEYS or not flag.startswith('--') or not tokens or key in values:
                raise ValueError('Unsupported HATPRO wrapper argument')
            values[key] = tokens.pop(0)
    if set(values) != set(KEYS) or values['start_fresh'] not in ('0', '1'):
        raise ValueError('Incomplete HATPRO source configuration')
    if values['state_file'] != '/var/lib/aurora-cloud/hatpro-sync.last':
        raise ValueError('Focused deployment requires the commissioned HATPRO state path')
    values['source_port'] = int(values['source_port'])
    values['start_fresh'] = values['start_fresh'] == '1'
    return values


def fingerprint(path, optional=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
        raise RuntimeError('Expected root-owned regular code/configuration file: ' + str(path))
    return dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                uid=info.st_uid, gid=info.st_gid, mode=stat.S_IMODE(info.st_mode))


def context():
    return {'config': configuration(WRAPPER.read_text()),
            'files': {str(WRAPPER): fingerprint(WRAPPER), str(DRIVER): fingerprint(DRIVER, True)},
            'protected': {str(path): fingerprint(path) for path in PROTECTED}}


def idle():
    result = subprocess.run(['systemctl', 'show', *UNITS,
                             '--property=Id,LoadState,ActiveState,MainPID,Job'],
                            capture_output=True, text=True, check=True, timeout=15)
    states = {}
    for block in result.stdout.strip().split('\n\n'):
        row = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if row.get('Id'):
            states[row['Id']] = row
    for unit in UNITS:
        row = states.get(unit, {})
        if (row.get('LoadState') != 'loaded' or row.get('ActiveState') not in ('inactive', 'failed')
                or row.get('MainPID') != '0' or row.get('Job') not in ('', '0')):
            raise RuntimeError('Deployment deferred; HATPRO source work is not idle: ' + unit)
    return states


def sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def exclusive(path, data):
    with path.open('xb') as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def replace(path, data, info):
    descriptor, temporary = tempfile.mkstemp(prefix='.hatpro-discovery-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            os.fchmod(handle.fileno(), info['mode'])
            os.fchown(handle.fileno(), info['uid'], info['gid'])
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def deploy(mode, release, expected, stage=None):
    if os.geteuid() != 0 or mode not in ('preflight', 'install'):
        raise RuntimeError('Root-owned focused deployment required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', release):
        raise ValueError('A unique safe release label is required')
    saved = BACKUPS / ('hatpro-discovery-' + release)
    info = BACKUPS.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('Expected existing restricted rollback parent')
    if saved.exists() or saved.is_symlink():
        raise RuntimeError('Never overwrite an existing rollback bundle')
    lock_path = Path(expected['config']['state_file']).parent / 'hatpro-backfill.lock'
    if not stat.S_ISREG(lock_path.lstat().st_mode):
        raise RuntimeError('Expected existing HATPRO lock inode')
    with lock_path.open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        units = idle()
        if context() != expected:
            raise RuntimeError('Live HATPRO inputs changed; deployment deferred')
        if mode == 'preflight':
            return {'state': 'idle'}
        stage = Path(stage)
        wrapper = (stage / WRAPPER.name).read_bytes()
        driver = (stage / DRIVER.name).read_bytes()
        compile(driver, str(DRIVER), 'exec')
        subprocess.run(['bash', '-n', str(stage / WRAPPER.name)], check=True)
        if configuration(wrapper.decode()) != expected['config']:
            raise RuntimeError('Staged source bindings differ from the deployed bindings')
        old = {path: path.read_bytes() if expected['files'][str(path)] else None for path in (WRAPPER, DRIVER)}
        saved.mkdir(mode=0o700)
        for path, data in old.items():
            if data is not None:
                backup = saved / (path.name + '.before')
                exclusive(backup, data)
                if fingerprint(backup)['sha256'] != expected['files'][str(path)]['sha256']:
                    raise RuntimeError('Rollback verification failed')
        receipt = dict(expected, units=units, recorded_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        exclusive(saved / 'before.json', (json.dumps(receipt, indent=2) + '\n').encode())
        sync_dir(saved)
        sync_dir(BACKUPS)
        idle()
        if context() != expected:
            raise RuntimeError('Live inputs changed after backup; deployment deferred')
        payload = {DRIVER: driver, WRAPPER: wrapper}
        changed = []
        try:
            for path, data in payload.items():
                meta = expected['files'][str(path)] or dict(uid=0, gid=0, mode=0o644)
                changed.append(path)
                replace(path, data, meta)
                actual = fingerprint(path)
                if actual != dict(meta, sha256=hashlib.sha256(data).hexdigest()):
                    raise RuntimeError('Installed file verification failed')
            if {str(path): fingerprint(path) for path in PROTECTED} != expected['protected']:
                raise RuntimeError('Protected writer/configuration/unit changed during deployment')
        except Exception:
            for path in reversed(changed):
                if old[path] is not None:
                    replace(path, old[path], expected['files'][str(path)])
                elif fingerprint(path)['sha256'] == hashlib.sha256(payload[path]).hexdigest():
                    path.unlink()  # Only this newly installed, hash-verified driver.
                    sync_dir(path.parent)
            raise
        installed = {str(path): fingerprint(path) for path in payload}
        exclusive(saved / 'installed.json', (json.dumps(installed, indent=2) + '\n').encode())
        sync_dir(saved)
        return {'state': 'installed', 'rollback': str(saved), 'files': installed}


if __name__ == '__main__':
    if sys.argv[1] == 'context':
        print(json.dumps(context()))
    else:
        print(json.dumps(deploy(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]),
                                sys.argv[4] if len(sys.argv) > 4 else None)))
