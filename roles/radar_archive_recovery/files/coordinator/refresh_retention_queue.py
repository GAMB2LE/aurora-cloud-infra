#!/usr/bin/env python3
"""Capture published mirror metadata and create a finite candidate queue.

The queue is advisory; it neither proves archive parity nor authorizes a
transfer or deletion. Each selected file needs fresh guarded admission.
"""
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

from prepare_direct_candidate import BASE, run_remote
from retention_queue import parse_manifest, rank

CAPTURE = r'''import pathlib,base64,hashlib,json,datetime
root=pathlib.Path('/data/aurora/internal/mirror_manifests/latest'); radar=root/'rpgfmcw94'
sig=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
root_before=root.stat();radar_before=radar.stat();entries={}
for name,path in [('summary.json',root/'summary.json'),('source.tsv',radar/'source.tsv'),('local.tsv',radar/'local.tsv'),('gws.tsv',radar/'gws.tsv')]:
 before=path.stat();raw=path.read_bytes();after=path.stat()
 if sig(before)!=sig(after) or len(raw)>16*1024*1024:raise ValueError('manifest changed or too large')
 entries[name]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':base64.b64encode(raw).decode()}
if sig(root_before)!=sig(root.stat()) or sig(radar_before)!=sig(radar.stat()):raise ValueError('published manifest changed')
print(json.dumps({'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'entries':entries},sort_keys=True))
'''

def build(capture, known, *, max_age_seconds=7200):
    if not isinstance(capture,dict) or set(capture.get('entries',{})) != {'summary.json','source.tsv','local.tsv','gws.tsv'}:
        raise ValueError('incomplete capture')
    files={}
    for name,record in capture['entries'].items():
        raw=base64.b64decode(record['bytes'],validate=True)
        if hashlib.sha256(raw).hexdigest()!=record['sha256']:raise ValueError('manifest hash differs')
        files[name]=raw
    summary=json.loads(files['summary.json']); radar=summary['streams']['rpgfmcw94']
    captured=datetime.datetime.fromisoformat(capture['captured_at']);observed=datetime.datetime.fromisoformat(summary['generated_at'])
    if captured.tzinfo is None or observed.tzinfo is None or not 0 <= (captured-observed).total_seconds() <= max_age_seconds:
        raise ValueError('mirror summary is stale or from the future')
    if summary.get('gws_available') is not True:raise ValueError('GWS observation unavailable')
    source=parse_manifest(files['source.tsv']);local=parse_manifest(files['local.tsv'],allow_other=True);gws=parse_manifest(files['gws.tsv'],allow_other=True)
    if any(radar.get(key)!=len(rows) for key,rows in [('source_count',source),('local_count',local),('gws_count',gws)]):
        raise ValueError('manifest counts disagree with published summary')
    cutoff=int(observed.timestamp())-7*86400
    candidates=rank(source,local,gws,known,cutoff=cutoff,maximum=1000)
    result={'captured_at':capture['captured_at'],'mirror_started_at':summary['generated_at'],
            'manifest_sha256':{name:hashlib.sha256(raw).hexdigest() for name,raw in files.items()},
            'cutoff_epoch':cutoff,'known_path_count':len(known),'candidate_count':len(candidates),'candidates':candidates,
            'scope':'advisory metadata candidates; per-file admission required; no parity or completeness proof',
            'eviction_authorized':False,'retention_authorized':False}
    return files,result

def main():
    capture=json.loads(run_remote(['python3','-c',CAPTURE],timeout=60).stdout)
    database=sqlite3.connect('file:'+str(BASE/'working-ledger.sqlite')+'?mode=ro',uri=True)
    try:
        database.execute('BEGIN')
        known={row[0] for table in ('provisional_object_versions','provisional_gws_versions')
               for row in database.execute('SELECT DISTINCT relative_path FROM '+table)}
    finally:database.close()
    files,result=build(capture,known)
    name=datetime.datetime.now(datetime.timezone.utc).strftime('retention-queue-%Y%m%dT%H%M%SZ')
    out=BASE/name;out.mkdir(mode=0o700)
    for filename,raw in files.items():
        fd=os.open(out/filename,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    fd=os.open(out/'queue.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(result,f,indent=2,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
    print(json.dumps({'queue':str(out/'queue.json'),'candidate_count':len(result['candidates']),
                      'mirror_started_at':result['mirror_started_at'],'retention_authorized':False,'eviction_authorized':False}))

if __name__=='__main__':main()
