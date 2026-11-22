"""Independent bounded byte readbacks for the existing signed retention selector."""
from contextlib import closing
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import runpy
import shlex
import stat
import subprocess
import sys

sys.path.insert(0, '/var/lib/aurora-radar-coordinator-v1/overnight-20261114')
from verify_native_archive_pairs_exact import CATALOG, _client, object_identity
from restore_verified_gws_primary import SSH

GWS = '/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/'
FIELD = ['sudo', '-n', '-u', 'aurora', 'ssh', '-oBatchMode=yes',
         '-oConnectTimeout=15', 'root@100.124.55.22']
HASH_CODE = '''from pathlib import Path
import hashlib,json,os,stat,sys
p=Path(sys.argv[1]);s=p.lstat()
if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1: raise ValueError('source is not regular single-link file')
with os.fdopen(os.open(p,os.O_RDONLY|os.O_NOFOLLOW),'rb') as f:
 h=hashlib.sha256()
 for b in iter(lambda:f.read(1024**2),b''):h.update(b)
 a=os.fstat(f.fileno())
identity=lambda x:(x.st_dev,x.st_ino,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
if identity(s)!=identity(a) or identity(a)!=identity(p.lstat()): raise ValueError('source changed')
print(json.dumps({'sha256':h.hexdigest(),'size':s.st_size,'mtime':s.st_mtime_ns//10**9}))
'''


def select(limit=5):
    module = runpy.run_path('/usr/local/bin/aurora-ass-retention', run_name='readonly_selector')
    summary = json.loads((module['MIRROR'] / 'summary.json').read_text())
    stamp = module['timestamp'](summary['generated_at'])
    snapshot = module['MIRROR'].parent / 'history' / stamp.strftime('%Y%m%dT%H%M%SZ')
    groups = {}
    streams = {s['name']: s for s in module['STREAMS']}
    for stream in module['STREAMS']:
        if not stream.get('prune_enabled'):
            continue
        name = stream['name']
        allowed = set((snapshot / name / 'prune_candidates.tsv').read_text().splitlines()[1:])
        cutoff = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=stream['retention_days'])).timestamp())
        groups[name] = []
        with (snapshot / name / 'source.tsv').open() as handle:
            for row in csv.DictReader(handle, delimiter='\t'):
                relative = row['relpath']
                if (relative.startswith('/') or any(p in ('', '.', '..') for p in relative.split('/'))
                        or any(ord(c) < 32 for c in relative)):
                    raise ValueError('unsafe source manifest path')
                if relative in allowed and int(row['mtime']) < cutoff:
                    groups[name].append({'stream': name, 'path': str(Path(stream['source_root']) / relative),
                                         'size': int(row['size']), 'mtime': int(row['mtime'])})
    selected = module['interleave_candidates'](groups, limit)
    for row in selected:
        stream = streams[row['stream']]
        row['relative_path'] = str(Path(row['path']).relative_to(stream['source_root']))
        row['archive_relative_path'] = str(Path(stream['archive_relpath']) / row['relative_path'])
        row['primary_path'] = str(Path(stream['cloud_root']) / row['relative_path'])
    return snapshot, selected


def primary(row, expected):
    path = Path(row['primary_path'])
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_size != row['size']
            or before.st_mtime_ns // 10**9 != row['mtime']):
        raise ValueError('primary metadata differs')
    value = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError('primary replaced before opening')
        for block in iter(lambda: stream.read(1024**2), b''):
            value.update(block)
        after = os.fstat(stream.fileno())
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if signature(before) != signature(after) or signature(after) != signature(path.lstat()) or value.hexdigest() != expected:
        raise ValueError('primary bytes differ or source version changed')


def archives(client, row, expected):
    gws = GWS + row['archive_relative_path']
    result = subprocess.check_output([*SSH, 'sha256sum', '--', gws], text=True, timeout=300).split()
    if result != [expected, gws]:
        raise ValueError('GWS bytes differ')
    address = {'Bucket': 'gamb2le-o', 'Key': 'data/incoming/aurora-cloud/raw/' + row['archive_relative_path']}
    before = object_identity(client.head_object(**address))
    if before[0] != row['size']:
        raise ValueError('object size differs')
    response = client.get_object(**address)
    value, size = hashlib.sha256(), 0
    with closing(response['Body']) as body:
        if object_identity(response) != before:
            raise ValueError('object changed before readback')
        for block in body.iter_chunks(1024**2):
            value.update(block); size += len(block)
            if size > row['size']:
                raise ValueError('object exceeds expected size')
    if size != row['size'] or value.hexdigest() != expected or object_identity(client.head_object(**address)) != before:
        raise ValueError('object bytes differ or version changed')


def save(path, record):
    with Path(path).open('x') as stream:
        json.dump(record, stream, sort_keys=True)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())


def verify(output, rows, snapshot):
    client = _client(json.loads(CATALOG.read_text()))
    for row in rows:
        source = json.loads(subprocess.check_output([*FIELD,
            shlex.join(['python3', '-c', HASH_CODE, row['path']])], text=True, timeout=300))
        if (source['size'], source['mtime']) != (row['size'], row['mtime']):
            raise ValueError('source differs from selected candidate')
        expected = source['sha256']
        primary(row, expected)
        archives(client, row, expected)
        row['sha256'] = expected
        row['source_primary_gws_object_sha256_verified'] = True
    save(output, {'schema': 'aurora-window-sample-readback-v1',
                  'verified_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'mirror_snapshot': str(snapshot), 'candidates': rows,
                  'retention_authorized': False, 'signed_standard_service_required': True})


def verify_survivors(preflight, output):
    proof = json.loads(Path(preflight).read_text())
    client = _client(json.loads(CATALOG.read_text()))
    for row in proof['candidates']:
        primary(row, row['sha256'])
        archives(client, row, row['sha256'])
    save(output, {'schema': 'aurora-window-post-retention-readback-v1',
                  'verified_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'all_sample_primary_gws_object_bytes_survived': True,
                  'sample_count': len(proof['candidates'])})
