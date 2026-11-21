#!/usr/bin/env python3
"""Prepare a single GWS read-only admission from reconciled object evidence."""
import argparse
import datetime
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import tarfile
import time
from prepare_direct_candidate import BASE, INFRA, ROOT, run_remote, verify_remote_hashes

REMOTE = '/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027'

def prepare(direct_name):
    if not re.fullmatch(r'20\d{6}T\d{4}(?:\d{2})?Z', direct_name):
        raise ValueError('invalid direct preparation name')
    direct = BASE/('direct-object-gzip-preparation-'+direct_name)
    request_raw=(direct/'request.json').read_bytes()
    request = json.loads(request_raw)
    operation = request['operation_id']
    if not re.fullmatch(r'20261027-gzip-next-\d{4}(?:\d{2})?z-0',operation):
        raise ValueError('invalid direct operation')
    events = [json.loads(line) for line in (direct/'ledger-reconcile/reconcile.jsonl').read_text().splitlines()]
    if not events or events[-1].get('phase') != 'complete' or events[-1].get('retention_authorized') is not False:
        raise ValueError('object evidence has not been reconciled')
    if events[0].get('phase') != 'intent' or events[0].get('request_sha256') != hashlib.sha256(request_raw).hexdigest():
        raise ValueError('reconciled request differs')
    name = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = BASE/('object-gws-gzip-preparation-'+name); out.mkdir(mode=0o700)
    accept = (ROOT/'output/aurora-archive-repair-20261026/release-20261027T005416Z/storage-review-release-completed-accepted.json').read_bytes()
    code = (INFRA/'scripts/verify_released_radar_continuity.py').read_text()
    raw = run_remote(['sudo','-n','python3','-c',code,'--acceptance-sha256',hashlib.sha256(accept).hexdigest()],data=accept).stdout
    (out/'continuity.json').write_bytes(raw); continuity=json.loads(raw)
    if continuity.get('accepted') is not True or continuity.get('files') != 640:
        raise ValueError('historical continuity refused')
    receipt=REMOTE+'/direct-object-v1/transfer-'+operation+'.jsonl.readback.json'
    raw=run_remote(['sudo','-n','cat',receipt],timeout=30).stdout
    if (events[0].get('receipt_sha256') != hashlib.sha256(raw).hexdigest()
            or raw != (direct/'ledger-reconcile/object-readback.json').read_bytes()):
        raise ValueError('reconciled object receipt differs')
    (out/'object-receipt.json').write_bytes(raw)
    if json.loads(raw)['expected']['relative_path'] != request['relative_path']:
        raise ValueError('object receipt path differs')
    request['operation_id']='20261027-gws-gzip-next-'+name[9:].lower()+'-0'
    source=request['expected_source']
    observation={'relative_path':request['relative_path'],'size':source['identity'][2],
                 'mtime_seconds':source['identity'][3]//1000000000,'observed_at':time.time()}
    for filename,value in [('request.json',request),('observation.json',observation)]:
        p=out/filename;p.write_text(json.dumps(value,sort_keys=True)+'\n');p.chmod(0o600)
    names=['request.json','observation.json','continuity.json']
    hashes={n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in names}
    archive=io.BytesIO()
    with tarfile.open(fileobj=archive,mode='w') as tar:
        for n in names:tar.add(out/n,arcname=n)
    directory=REMOTE+'/object-gws-v1/input-gzip-'+name.lower()
    command=('umask 077; mkdir '+shlex.quote(directory)+' && tar -xf - -C '+shlex.quote(directory)
             +' && chown root:root '+shlex.quote(directory)+'/*.json && chmod 600 '+shlex.quote(directory)
             +'/*.json && sha256sum '+shlex.quote(directory)+'/*.json')
    result=run_remote(['sudo','-n','sh','-c',command],data=archive.getvalue())
    verify_remote_hashes(result.stdout,directory,hashes)
    object_request=REMOTE+'/direct-object-gzip-supervisor-v2/input-'+direct_name.lower()+'/request.json'
    args=['/usr/bin/python3',REMOTE+'/object-gws-v1/object_gws_gzip_new_target_launcher.py',
          '--request',directory+'/request.json','--request-sha256',hashes['request.json'],
          '--observation',directory+'/observation.json','--observation-sha256',hashes['observation.json'],
          '--continuity',directory+'/continuity.json','--continuity-sha256',hashes['continuity.json'],
          '--object-receipt',receipt,'--object-receipt-sha256',hashlib.sha256(raw).hexdigest(),
          '--object-request',object_request,'--object-request-sha256',hashlib.sha256((direct/'request.json').read_bytes()).hexdigest()]
    (out/'launcher-args.json').write_text(json.dumps(args)+'\n')
    unit='aurora-gws-gzip-admission-'+name.lower()
    run_remote(['sudo','-n','systemd-run','--unit='+unit,'--property=Type=exec',
                '--property=RemainAfterExit=yes','--property=RuntimeMaxSec=20min','--nice=15',*args])
    return {'out':str(out),'operation':request['operation_id'],'admission_unit':unit+'.service',
            'transfer_started':False,'eviction_authorized':False,'retention_authorized':False}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('direct_name');args=parser.parse_args()
    print(json.dumps(prepare(args.direct_name),sort_keys=True))
