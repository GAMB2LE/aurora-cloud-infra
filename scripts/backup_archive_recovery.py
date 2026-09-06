#!/usr/bin/env python3
"""Create and verify a restricted, scoped pre-cutover recovery bundle (root)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time


def digest(handle):
    value = hashlib.sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b''):
        value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--dashboard-only', action='store_true')
    args = parser.parse_args()
    root = Path(args.destination).resolve()
    allowed = Path('/var/lib/aurora-cloud/recovery-rollbacks')
    if root.parent != allowed or root.exists() or os.geteuid() != 0:
        parser.error('requires root and a new named child of /var/lib/aurora-cloud/recovery-rollbacks')
    os.umask(0o077)
    root.mkdir(parents=True, mode=0o700)
    catalog_path = Path('/etc/aurora-object-store/catalog.json')
    catalog = json.loads(catalog_path.read_text())
    units = ['aurora-object-store-inventory.service', 'aurora-object-store-inventory-incremental@raw.service',
             'aurora-object-store-repair.service', 'aurora-object-store-recheck-after-repair.service',
             'aurora-ass-retention.service']
    for unit in units:
        result = subprocess.run(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'],
                                check=True, capture_output=True, text=True)
        if result.stdout.strip() in ('active', 'activating', 'deactivating'):
            raise SystemExit('Not an idle boundary: ' + unit)
    paths = [catalog_path, Path(catalog['manifest_root']) / 'latest',
             Path('/var/lib/aurora-cloud/object-store-verification-gate'),
             Path('/usr/local/bin/aurora-ass-retention'),
             Path('/usr/local/bin/aurora-archive-health'),
             Path('/opt/aurora-cloud-dashboard/mobile_catalog.py')]
    for directory, pattern in [('/etc/systemd/system', 'aurora-object-store-*'),
                               ('/usr/local/bin', 'aurora-object-store-*'),
                               ('/usr/local/sbin', 'aurora-object-store-*')]:
        paths.extend(sorted(Path(directory).glob(pattern)))
    if args.dashboard_only:
        paths=[Path('/opt/aurora-cloud-dashboard/mobile_catalog.py'),Path('/opt/aurora-cloud-dashboard/test_mobile_catalog.py')]
    dashboard_revision=subprocess.run(['git','-C','/opt/aurora-cloud-dashboard','rev-parse','HEAD'],check=True,capture_output=True,text=True).stdout.strip()
    archive = root / 'before.tar.gz'
    records = {}
    with tarfile.open(archive, 'w:gz', compresslevel=1) as tar:
        for path in paths:
            if path.exists() or path.is_symlink():
                tar.add(path, arcname=str(path).lstrip('/'))
    # Verify the full archived payload, not just tar's exit status. Each file
    # must still match its source at this idle boundary.
    with tarfile.open(archive, 'r:gz') as tar:
        for member in tar:
            if member.isfile():
                with tar.extractfile(member) as handle:
                    archived = digest(handle)
                with Path('/' + member.name).open('rb') as handle:
                    current = digest(handle)
                if archived != current:
                    raise SystemExit('Source changed while backing up: ' + member.name)
                records[member.name] = archived
    with archive.open('rb') as handle:
        sha = digest(handle)
    states = subprocess.run(['systemctl', 'list-timers', '--all', '--no-pager', 'aurora-object-store*'],
                            check=True, capture_output=True, text=True).stdout
    state = {'created_at': time.time(), 'archive_sha256': sha, 'verified_files': records,'dashboard_revision':dashboard_revision,
             'requested_paths': [str(p) for p in paths], 'timer_state': states,
             'latest_was_symlink': (Path(catalog['manifest_root']) / 'latest').is_symlink()}
    (root / 'manifest.json').write_text(json.dumps(state, indent=2) + '\n')
    print(json.dumps({'bundle': str(archive), 'sha256': sha, 'verified_files': len(records),
                      'bytes': archive.stat().st_size}))


if __name__ == '__main__':
    main()
