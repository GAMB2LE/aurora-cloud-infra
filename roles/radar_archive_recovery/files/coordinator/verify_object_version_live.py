#!/usr/bin/env python3
"""Read an exact recorded object version and verify stable metadata and bytes."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from gws_pending_replay import valid_expected
from prepare_direct_candidate import BASE, run_remote
from provisional_object_evidence import BASE as OBJECT_BASE

REMOTE_CODE = r'''import datetime,hashlib,json,sys
sys.path.insert(0,'/usr/local/lib/aurora-object-store')
from aurora_object_store_s3 import _client
expected=json.loads(sys.argv[1]);key=sys.argv[2]
config=json.load(open('/etc/aurora-object-store/catalog.json'));c=_client(config)
prefix='gamb2le-object-store:gamb2le-o/'
if not key.startswith(prefix):raise ValueError('unexpected object namespace')
s3key=key[len(prefix):]
def metadata():
 r=c.head_object(Bucket='gamb2le-o',Key=s3key)
 return {'key':key,'size':r['ContentLength'],'etag':r.get('ETag'),
         'last_modified':str(r.get('LastModified')),'version_id':r.get('VersionId')}
before=metadata()
if before['size']!=expected['size'] or not before['etag'] or not before['last_modified']:raise ValueError('object metadata differs')
obj=c.get_object(Bucket='gamb2le-o',Key=s3key);body=obj['Body'];digest=hashlib.sha256();count=0
try:
 while True:
  data=body.read(min(1024*1024,expected['size']-count+1))
  if not data:break
  count+=len(data)
  if count>expected['size']:raise ValueError('object exceeds expected size')
  digest.update(data)
finally:body.close()
after=metadata()
if before!=after or count!=expected['size'] or digest.hexdigest()!=expected['sha256']:raise ValueError('object changed or differs')
if obj.get('ETag')!=before['etag'] or obj.get('VersionId')!=before['version_id']:raise ValueError('GET identity differs')
print(json.dumps({'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'expected':expected,
 'object_key':key,'metadata_before':before,'metadata_after':after,'readback':{'read_bytes':count,'sha256':digest.hexdigest()},
 'eviction_authorized':False,'retention_authorized':False}))
'''


def read_exact(expected, key):
    if not (valid_expected(expected) and isinstance(key, str) and re.fullmatch(
            re.escape(OBJECT_BASE)+r'[0-9a-f]{32}/'+expected['sha256']+r'\.radar', key)):
        raise ValueError('object readback identity differs')
    result = json.loads(run_remote(['sudo','-n','-u','aurora','--','/opt/aurora-object-store-recovery/venv/bin/python',
                                    '-c',REMOTE_CODE,json.dumps(expected,sort_keys=True),key], timeout=900).stdout)
    if not (result.get('expected') == expected and result.get('object_key') == key
            and result.get('metadata_before') == result.get('metadata_after')
            and result.get('readback') == {'read_bytes':expected['size'],'sha256':expected['sha256']}
            and result.get('eviction_authorized') is False and result.get('retention_authorized') is False):
        raise ValueError('object proof differs')
    return result


def main(receipt_sha):
    if not re.fullmatch('[0-9a-f]{64}', receipt_sha): raise ValueError('invalid receipt hash')
    db = sqlite3.connect('file:'+str(BASE/'working-ledger.sqlite')+'?mode=ro', uri=True)
    try:
        rows = db.execute('SELECT relative_path,size,mtime_ns,content_sha256,object_key,receipt_json FROM provisional_object_versions WHERE receipt_sha256=?', (receipt_sha,)).fetchall()
    finally: db.close()
    if len(rows) != 1: raise ValueError('object receipt missing or ambiguous')
    path,size,mtime,sha,key,receipt = rows[0]
    expected = {'relative_path':path,'size':size,'mtime_ns':mtime,'sha256':sha}
    if not (valid_expected(expected) and hashlib.sha256(receipt).hexdigest() == receipt_sha
            and isinstance(key,str) and re.fullmatch(re.escape(OBJECT_BASE)+r'[0-9a-f]{32}/'+sha+r'\.radar',key)):
        raise ValueError('recorded object identity differs')
    result = read_exact(expected, key)
    now = datetime.datetime.now(datetime.timezone.utc)
    out = BASE/('object-live-readback-'+now.strftime('%Y%m%dT%H%M%SZ')+'.json')
    fd = os.open(out, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd,'w') as f: json.dump(result,f,sort_keys=True,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    fd = os.open(BASE, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)
    print(json.dumps({'evidence':str(out),'object_verified':True,'eviction_authorized':False,'retention_authorized':False}))
    return out,result


if __name__ == '__main__': main(sys.argv[1])
