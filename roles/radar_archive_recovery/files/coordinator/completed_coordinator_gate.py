"""Allow the existing archive lanes after proven historical completion."""
import contextlib
import subprocess
from pathlib import Path


def require_completed():
    from per_file_coordinator import BASE, STATE, load_candidates
    from per_file_queue import Queue
    from audit_frozen_cohort import obligations, ledger_report
    raw = subprocess.run(['systemctl', 'show', 'aurora-radar-coordinator.service',
        '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID', '-p', 'Result',
        '-p', 'ExecMainStatus', '-p', 'ExecStart'], check=True, capture_output=True,
        text=True, timeout=20).stdout
    props = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    program = '/var/lib/aurora-radar-coordinator-v1/finalized-coordinator-project-override-v3/coordinator/per_file_coordinator.py'
    if (props.get('ActiveState') != 'inactive' or props.get('SubState') != 'dead'
            or props.get('MainPID') != '0' or props.get('Result') != 'success'
            or props.get('ExecMainStatus') != '0'
            or 'argv[]=/usr/bin/python3 ' + program + ' run ' not in props.get('ExecStart', '')):
        raise ValueError('historical coordinator completion is unproven')
    manifest = BASE/'retention-queue-20261107T204846Z/queue.json'
    candidates = load_candidates(manifest, 'bb70cfae9682f385f3d3b9d321eb58bc090b331f196c6bc8bfe2a75fd6c0e7ac')
    with contextlib.closing(Queue(STATE, readonly=True)) as queue:
        count, unfinished = queue.db.execute('SELECT count(*),sum(stage!=4) FROM files').fetchone()
        if queue.outstanding() is not None or count < 750 or unfinished != 0:
            raise ValueError('historical queue completion is unproven')
        import json
        completed = {(v['relative_path'], v['size'], v['mtime_ns']//10**9)
                     for row in queue.db.execute('SELECT identity FROM files WHERE stage=4')
                     for v in [json.loads(row[0])]}
        if len(candidates) != 566 or any((r['relative_path'],r['size'],r['mtime']) not in completed for r in candidates):
            raise ValueError('frozen historical versions are not checkpointed')
    items, _ = obligations(manifest, BASE/'interrupted-batch-handoff-20261107T203613Z.json')
    report = ledger_report(items)
    if len(items) != 581 or len(report) != 581 or any(row['state'] != 'pair-recorded' for row in report):
        raise ValueError('frozen exact archive pairs are incomplete')
