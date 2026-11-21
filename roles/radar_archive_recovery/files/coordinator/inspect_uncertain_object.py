#!/usr/bin/env python3
"""Read-only reconciliation observation for one failed object upload."""
import datetime
import json
import os
from pathlib import Path

from bounded_backlog_runner import state
from prepare_direct_candidate import BASE, run_remote

REMOTE_CODE = r'''import datetime,json,sys
sys.path.insert(0,'/usr/local/lib/aurora-object-store')
from aurora_object_store_s3 import _client
from botocore.exceptions import ClientError
config=json.load(open('/etc/aurora-object-store/catalog.json'));c=_client(config)
bucket='gamb2le-o';prefix='data/internal/aurora-cloud/radar-archive-first/versions/v1/1e537758958a82f9a2cb0273936d99a7/'
key=prefix+'d6acfa8a819096da7687c15cdc97f206d71692c41054f7f9b3596ba989bf87a1.radar'
control='data/internal/aurora-cloud/radar-archive-inventory/v1/replicas/6b262b24ec9dcac299cca2292aba9dc9e7cbef74ad96c9dc30e9459c9164c665.7b70acaf6ad06b0776c8e1d1b1c6bb8e.sqlite'
checks=[]
for _ in range(2):
 a=c.head_object(Bucket=bucket,Key=control)
 try:
  h=c.head_object(Bucket=bucket,Key=key);status=h['ResponseMetadata']['HTTPStatusCode']
 except ClientError as exc:status=exc.response.get('ResponseMetadata',{}).get('HTTPStatusCode')
 listing=c.list_objects_v2(Bucket=bucket,Prefix=prefix,MaxKeys=1000)
 multipart=c.list_multipart_uploads(Bucket=bucket,Prefix=prefix,MaxUploads=1000)
 checks.append({'control_http':a['ResponseMetadata']['HTTPStatusCode'],'control_size':a['ContentLength'],
                'target_http':status,'list_http':listing['ResponseMetadata']['HTTPStatusCode'],
                'list_truncated':listing.get('IsTruncated'),'objects':[v['Key'] for v in listing.get('Contents',[])],
                'multipart_http':multipart['ResponseMetadata']['HTTPStatusCode'],
                'multipart_truncated':multipart.get('IsTruncated'),
                'uploads':[{'key':v['Key'],'upload_id':v['UploadId']} for v in multipart.get('Uploads',[])]})
print(json.dumps({'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'prefix':prefix,'key':key,'checks':checks}))
'''


def main():
    marker = json.loads((BASE/'bounded-backlog-unresolved.json').read_bytes())
    props = state(marker['service'])
    if not (props.get('ActiveState') == 'failed' and props.get('Result') == 'exit-code'
            and props.get('InvocationID') == marker['invocation_id']):
        raise ValueError('failed worker identity or terminal state differs')
    result = json.loads(run_remote(['sudo','-n','-u','aurora','--','timeout','60s',
                                    '/opt/aurora-object-store-recovery/venv/bin/python','-c',REMOTE_CODE], timeout=70).stdout)
    checks = result['checks']
    if not (len(checks) == 2 and all(c.get('control_http') == 200 and c.get('control_size') == 3428352
            and c.get('target_http') == 404 and c.get('list_http') == 200 and c.get('list_truncated') is False
            and c.get('objects') == [] and c.get('multipart_http') == 200 and c.get('multipart_truncated') is False
            and c.get('uploads') == [] for c in checks)):
        raise ValueError('failed upload not conclusively absent')
    result.update({'worker': props, 'marker': marker, 'scope': 'point-in-time read-only observation; failed key remains quarantined',
                   'eviction_authorized': False, 'retention_authorized': False})
    name = datetime.datetime.now(datetime.timezone.utc).strftime('failed-object-reconciliation-%Y%m%dT%H%M%SZ.json')
    path = BASE/name
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out: json.dump(result, out, sort_keys=True, indent=2); out.write('\n'); out.flush(); os.fsync(out.fileno())
    fd = os.open(BASE, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)
    print(json.dumps({'evidence':str(path),'failed_object_absent':True,'multipart_absent':True,
                      'eviction_authorized':False,'retention_authorized':False}))


if __name__ == '__main__': main()
