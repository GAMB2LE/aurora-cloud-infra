#!/usr/bin/env python3
"""Install the rehearsed layout checks atomically, with protected rollback copies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from radar_retention_maintenance import Maintenance, restore_current

LAYOUT = Path('/var/lib/aurora-radar-coordinator-v1/overnight-20261114/relocated-may-d13-cutover-candidate/verify_radar_mount_layout.py')
ACCEPTANCE = Path('/var/lib/aurora-radar-coordinator-v1/output/aurora-archive-repair-20261026/release-20261027T005416Z/storage-review-release-completed-accepted.json')
REGISTRY = Path('/etc/aurora/radar-retention-compressed.json')


def supported(path):
    return str(path).startswith('/var/lib/aurora-') or path == Path('/usr/local/lib/aurora-radar-retention-layout.py')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_stable(path, pin):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or path.resolve() != path:
        raise ValueError('unsafe release component: ' + str(path))
    raw = path.read_bytes()
    after = path.lstat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if sha(raw) != pin or identity(before) != identity(after):
        raise ValueError('release component changed: ' + str(path))
    return raw, {'uid': before.st_uid, 'gid': before.st_gid, 'mode': stat.S_IMODE(before.st_mode)}


def atomic(path, raw, metadata):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.retention-release-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); os.fchown(stream.fileno(), metadata['uid'], metadata['gid'])
            os.fchmod(stream.fileno(), metadata['mode']); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate():
    layout = json.loads(subprocess.check_output(['/usr/bin/python3', str(LAYOUT)], text=True, timeout=180))
    acceptance = ACCEPTANCE.read_bytes()
    if sha(acceptance) != 'f0edc78e2d99775dff9eff914f8eadd053e98b57f4e8346b8add8953d651f238':
        raise ValueError('historical release acceptance differs')
    result = subprocess.run(['/usr/bin/python3', str(LAYOUT.with_name('verify_released_radar_continuity.py')),
        '--acceptance-sha256', sha(acceptance)], input=acceptance, capture_output=True, check=False, timeout=300)
    if result.returncode:
        raise ValueError('normal historical continuity check failed: ' + result.stderr.decode()[:2000])
    continuity = json.loads(result.stdout)
    if continuity.get('accepted') is not True or continuity.get('files') != 640:
        raise ValueError('historical science cohort is incomplete')
    return {'layout': layout, 'historical_continuity': continuity}


def install(release):
    manifest_raw = (release / 'release.json').read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest.get('schema') != 'radar-retention-layout-release-v1':
        raise ValueError('unknown release contract')
    if REGISTRY.exists() and json.loads(REGISTRY.read_text()).get('routes'):
        raise ValueError('initial checker rollout must precede compressed day retirement')
    staged = {}
    for row in manifest['changes']:
        path = Path(row['path'])
        if not supported(path) or '..' in path.parts or path in staged:
            raise ValueError('unexpected or duplicate release path')
        raw, _ = read_stable(release / str(path).lstrip('/'), row['after_sha256'])
        compile(raw, str(path), 'exec')
        staged[path] = (row, raw)
    with Maintenance('layout-release') as window:
        backups = window.folder / 'backup'; backups.mkdir(mode=0o700)
        records = []
        # Validate the entire before-image set before changing a single component.
        for path, (row, raw) in staged.items():
            before = row['before_sha256']
            if before is None:
                if path.exists():
                    read_stable(path, row['after_sha256'])
                records.append({**row, 'preexisting_new_helper': path.exists()})
                continue
            original, metadata = read_stable(path, before)
            backup = backups / str(path).lstrip('/'); backup.parent.mkdir(parents=True, exist_ok=True)
            atomic(backup, original, {'uid': 0, 'gid': 0, 'mode': 0o400})
            records.append({**row, 'metadata': metadata, 'backup': str(backup)})
        window.state.update(release_path=str(release), manifest_sha256=sha(manifest_raw), components=records)
        window.save(); window.unsafe()
        try:
            for record in records:
                path = Path(record['path']); row, raw = staged[path]
                if row['before_sha256'] is None and path.exists():
                    continue
                if row['before_sha256'] is not None:
                    read_stable(path, row['before_sha256'])
                atomic(path, raw, record.get('metadata', {'uid': 0, 'gid': 0, 'mode': 0o755}))
            for record in records:
                read_stable(Path(record['path']), record['after_sha256'])
            # Retain all coordinator locks and paused timers, but enter the normal
            # shared-lease admission state for the unchanged continuity CLI.
            result = window.handoff_check(validate)
            window.state['verification'] = result; window.state['release_installed'] = True
            window.safe()
            print(json.dumps({'layout_release_installed': True, 'components': len(records),
                              'historical_files_verified': 640, 'operation': str(window.folder)}), flush=True)
        except BaseException:
            window.reenter_exclusive()
            for record in reversed(records):
                if record['before_sha256'] is None:
                    continue  # The unused helper is harmless alongside the old checks.
                path = Path(record['path'])
                current = sha(path.read_bytes())
                if current not in (record['before_sha256'], record['after_sha256']):
                    raise ValueError('another writer changed a component; keep maintenance held')
                original, _ = read_stable(Path(record['backup']), record['before_sha256'])
                atomic(path, original, record['metadata'])
            # handoff_check restored the owned barrier and exclusive lease on
            # failure; validate the rolled-back normal admission path as well.
            window.state['rollback_verification'] = window.handoff_check(validate)
            window.state['rolled_back'] = True; window.safe()
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('root release control required')
    if args.restore:
        restore_current()
    elif args.release:
        install(args.release)
    else:
        parser.error('select --release or --restore')
