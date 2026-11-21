#!/usr/bin/env python3
"""Back up the local GWS-only pending queue to GWS, without object claims."""
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sqlite3

from prepare_direct_candidate import BASE, INFRA


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def save(path, value):
    raw = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out: out.write(raw); out.flush(); os.fsync(out.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def main():
    ledger = load('radar_ledger_snapshot', INFRA/'roles/radar_archive_cache/files/aurora-radar-archive-ledger.py')
    publisher = load('radar_ledger_publisher', INFRA/'scripts/publish_radar_ledger_snapshot.py')
    root = BASE / 'gws-only-pending-snapshots'; root.mkdir(mode=0o700, exist_ok=True)
    db = sqlite3.connect('file:' + str(BASE/'gws-only-pending.sqlite') + '?mode=ro', uri=True)
    try:
        db.execute('BEGIN')
        rows = db.execute('SELECT relative_path,size,mtime_ns,sha256,evidence_json FROM pending').fetchall()
        count = len(rows)
        if not count: raise ValueError('empty pending queue')
        names = {'preflight.json', 'journal.jsonl', 'commit.jsonl', 'fence.json', 'readback.json'}
        for path, size, mtime, digest, evidence in rows:
            verification = json.loads(evidence)
            artifacts = db.execute('SELECT name,content_sha256,content FROM artifacts WHERE relative_path=? AND size=? AND mtime_ns=? AND sha256=?',
                                   (path, size, mtime, digest)).fetchall()
            if {a[0] for a in artifacts} != names or len(artifacts) != len(names):
                raise ValueError('pending version lacks complete evidence bundle')
            if any(hashlib.sha256(content).hexdigest() != sha or verification['evidence_sha256'].get(name) != sha
                   for name, sha, content in artifacts):
                raise ValueError('pending evidence bundle differs')
        snapshot = ledger.snapshot_database(db, root)
    finally: db.close()
    path = Path(snapshot['path']); sha = snapshot['snapshot_sha256']; raw = path.read_bytes()
    if len(raw) > publisher.MAX_SIZE or hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError('snapshot identity differs')
    name = datetime.datetime.now(datetime.timezone.utc).strftime('gws-only-pending-replica-%Y%m%dT%H%M%SZ')
    out = BASE/name; out.mkdir(mode=0o700)
    save(out/'intent.json', {'sha256': sha, 'size': len(raw), 'records': count,
                             'object_verified': False, 'eviction_authorized': False, 'retention_authorized': False})
    code = publisher.GWS_PUBLISH_CODE
    old = "('radar-archive-inventory','v1')"; new = "('gws-only-pending','v1')"
    if code.count(old) != 1: raise ValueError('publisher layout changed')
    code = code.replace(old, new)
    base = publisher.GWS_BASE
    ssh = ['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','IdentitiesOnly=yes',
           '-o','ConnectTimeout=15','-i','/home/aurora/.ssh/id_rsa_jasmin_20200514','rrniii@xfer-vm-03.jasmin.ac.uk']
    def remote(command, *, data=None):
        result = publisher.ssh(['sudo','-n','-u','aurora','--',*ssh,shlex.join(command)], data=data, timeout=180)
        if result.returncode: raise RuntimeError('GWS snapshot outcome uncertain; inspect before retry')
        return json.loads(result.stdout)
    result = remote(['python3','-c',code,base,sha,str(len(raw))], data=raw)
    if not (result.get('sha256') == sha and result.get('size') == len(raw) and result.get('atomic_no_replace') is True):
        raise ValueError('GWS publication receipt differs')
    save(out/'publication.json', result)
    target = base+'/gws-only-pending/v1/'+sha+'.sqlite'
    readback = remote(['python3','-c',publisher.GWS_VERIFY_CODE,target,sha,str(len(raw))])
    if not (readback.get('sha256') == sha and readback.get('size') == len(raw) and readback.get('readback_verified') is True):
        raise ValueError('GWS snapshot readback differs')
    result = {'path': target, 'sha256': sha, 'records': count, 'gws_readback_verified': True,
              'object_verified': False, 'eviction_authorized': False, 'retention_authorized': False}
    save(out/'verification.json', result)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__': main()
