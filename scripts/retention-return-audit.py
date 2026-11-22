#!/usr/bin/env python3
"""Read-only release assessment; this tool never authorizes deletion."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        return {"read_error": str(exc)}


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    return {"returncode": result.returncode, "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()}


def main():
    now = dt.datetime.now(dt.timezone.utc)
    gate = read('/var/lib/aurora-cloud/object-store-verification-gate/state.json')
    raw = gate.get('domains', {}).get('raw_retention', {})
    floor = raw.get('evidence_floor_generated_at')
    fresh = False
    if floor:
        age = (now - dt.datetime.fromisoformat(floor.replace('Z', '+00:00'))).total_seconds()
        fresh = 0 <= age < 8 * 3600
    days = []
    root = Path('/data/aurora/raw-relocated/rpgfmcw94')
    for day in sorted(root.glob('Y*/M*/D*')):
        files = []
        for path in sorted(day.iterdir()):
            s = path.lstat()
            files.append({'name': path.name, 'bytes': s.st_size,
                          'allocated_bytes': s.st_blocks * 512,
                          'mtime_ns': s.st_mtime_ns, 'inode': s.st_ino,
                          'device': s.st_dev, 'mode': s.st_mode,
                          'links': s.st_nlink})
        days.append({'day': str(day.relative_to(root)), 'files': files,
                     'allocated_bytes': sum(f['allocated_bytes'] for f in files)})
    holds = {str(p): p.read_text() for p in Path('/etc/aurora').glob('*hold')}
    disks = {}
    for path in ('/', '/data', '/project'):
        s = os.statvfs(path)
        disks[path] = {'total_bytes': s.f_blocks * s.f_frsize,
                       'free_bytes': s.f_bfree * s.f_frsize,
                       'available_bytes': s.f_bavail * s.f_frsize}
    helpers = []
    for base in (Path('/var/lib/aurora-deadline-repair-20261120'),
                 Path('/var/lib/aurora-radar-coordinator-v1/overnight-20261114')):
        for p in sorted(base.glob('closed*mount.py')):
            text = p.read_text()
            if 'ass-retention.hold' in text:
                helpers.append(str(p))
    trials = {}
    for name in ('retention-canary-independent-audit.json',
                 'retention-bulk-independent-audit.json'):
        data = read('/var/lib/aurora-deadline-repair-20261120/' + name)
        trials[name] = {k: v for k, v in data.items()
                        if not isinstance(v, (dict, list))}
    result = {'schema': 'aurora-retention-return-assessment-v1',
              'generated_at': now.isoformat(), 'read_only': True,
              'deletion_authorized': False, 'disks': disks,
              'raw_gate': raw, 'raw_evidence_fresh': fresh,
              'raw_retention_ready': gate.get('raw_retention_ready'),
              'writers_policy': gate.get('writers_policy'),
              'archive_health': read('/data/aurora/internal/archive_status/health-v1.json').get('operator_status'),
              'holds': holds, 'radar_days': days,
              'radar_helpers_requiring_hold': helpers,
              'previous_trials': trials,
              'retention_unit': command('systemctl', 'cat', 'aurora-ass-retention.service'),
              'retention_timer': command('systemctl', 'show', 'aurora-ass-retention.timer',
                  '-p', 'ActiveState', '-p', 'UnitFileState', '-p', 'NextElapseUSecRealtime'),
              'mounts': command('findmnt', '--json', '-o', 'TARGET,SOURCE,FSTYPE,OPTIONS'),
              'release_blockers': ['cloud retirement has not been commissioned',
                                   'radar mount hold dependencies need migration',
                                   'fresh bounded trial and first scheduled runs remain unverified']}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
