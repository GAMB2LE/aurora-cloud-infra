#!/usr/bin/env python3
"""Prepare and start one read-only admission, never a science transfer."""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import subprocess
import tarfile
import time
import gws_object_release

ROOT = Path(os.environ.get('RADAR_COORDINATOR_ROOT', '/Users/rrniii/Documents/GAMB2LE'))
BASE = ROOT/'output/aurora-archive-repair-20261026/archive-first-implementation-20261027'
INFRA = ROOT/'tmp/radar-native-backfill-admission-20261026'
REMOTE = '/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/direct-object-gzip-supervisor-v2'
GWS_OBJECT_LAUNCHER = str(Path(REMOTE).parent/'gws-object-source-v1/gws_object_launcher.py')
GWS_OBJECT_PINS = {
    GWS_OBJECT_LAUNCHER: '65df87e032734a309b2a99e73c10f94e898ba1eaa2dddca2329375317e64bb00',
    str(Path(GWS_OBJECT_LAUNCHER).with_name('gws_object_cloud_adapter.py')):
        '3733230c5e5f982c5e7b0ea436550525a076f0ac75f2b3451546f68bde7e3483',
}
PATH = re.compile(r'Y\d{4}/M\d{2}/D\d{2}/[A-Za-z0-9_.-]+\.(?:LV0|LV0\.NC|LV1|LV1\.NC)\Z')
def gateway(command):
    import sys
    sys.path.insert(0, str(INFRA/'scripts'))
    from radar_gateway import gateway as build
    return build(command, connect_timeout=8)

def verify_remote_hashes(raw, directory, hashes):
    lines=raw.decode('ascii').splitlines()
    observed={}
    for line in lines:
        fields=line.split(None,1)
        if len(fields)!=2 or not re.fullmatch('[0-9a-f]{64}',fields[0]):
            raise ValueError('invalid remote checksum output')
        path=fields[1].lstrip('*')
        prefix=directory+'/'
        if not path.startswith(prefix):raise ValueError('unexpected remote input path')
        name=path[len(prefix):]
        if name in observed:raise ValueError('duplicate remote checksum')
        observed[name]=fields[0]
    if observed!=hashes:raise ValueError('remote inputs differ')

def run_remote(command, *, data=None, timeout=180):
    return subprocess.run(gateway(command), input=data, capture_output=True, check=True, timeout=timeout)

def prepare(relative, *, gws_source=False):
    if not isinstance(relative, str) or not PATH.fullmatch(relative):
        raise ValueError('invalid radar path')
    release = gws_object_release.select() if gws_source else None
    name = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stamp = name.lower() if gws_source else name[9:].lower()
    operation = '20261027-gzip-next-'+stamp+'-0'
    out = BASE/('direct-object-gzip-preparation-'+name)
    out.mkdir(mode=0o700)
    source_code = runpy.run_path(str(INFRA/'scripts/aurora_radar_bounded_canary.py'))['source_code']()
    inner = ['sudo','-n','-u','aurora','ssh','-o','BatchMode=yes','-o','ConnectTimeout=12',
             '-o','IdentitiesOnly=yes','-o','IdentityFile=none','-o','PubkeyAuthentication=no',
             '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile=/home/aurora/.ssh/known_hosts',
             'aurora@100.124.55.22',shlex.join(['python3','-c',source_code,'/home/aurora/data/rpgfmcw94/'+relative])]
    raw = run_remote(inner).stdout
    (out/'source-hash.json').write_bytes(raw); source = json.loads(raw)
    accept = (ROOT/'output/aurora-archive-repair-20261026/release-20261027T005416Z/storage-review-release-completed-accepted.json').read_bytes()
    code = (INFRA/'scripts/verify_released_radar_continuity.py').read_text()
    raw = run_remote(['sudo','-n','python3','-c',code,'--acceptance-sha256',hashlib.sha256(accept).hexdigest()], data=accept).stdout
    (out/'continuity.json').write_bytes(raw); continuity=json.loads(raw)
    if continuity.get('accepted') is not True or continuity.get('files') != 640:
        raise ValueError('historical continuity refused')
    request = json.loads((BASE/'direct-object-gzip-preparation-20261106T2249Z/request.json').read_text())
    request.update(expected_source=source, relative_path=relative, operation_id=operation)
    # Reviewed ASFS lifetime-guard release; historical requests remain unchanged.
    request["archive_dispatch_sha256"] = "750bcaad0881f057e6f8f2cd1f9c6ef298182c8aa5abc69d45e0c31947ba8043"
    observation = {'relative_path':relative,'size':source['identity'][2],
                   'mtime_seconds':source['identity'][3]//1000000000,'observed_at':time.time()}
    for filename, value in [('request.json',request),('observation.json',observation)]:
        p=out/filename; p.write_text(json.dumps(value,sort_keys=True)+'\n'); p.chmod(0o600)
    names=['request.json','observation.json','continuity.json']
    hashes={n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in names}
    archive=io.BytesIO()
    with tarfile.open(fileobj=archive,mode='w') as tar:
        for n in names: tar.add(out/n,arcname=n)
    directory=REMOTE+'/input-'+name.lower()
    command=('umask 077; mkdir '+shlex.quote(directory)+' && tar -xf - -C '+shlex.quote(directory)
             +' && chown root:root '+shlex.quote(directory)+'/*.json && chmod 600 '+shlex.quote(directory)
             +'/*.json && sha256sum '+shlex.quote(directory)+'/*.json')
    result=run_remote(['sudo','-n','sh','-c',command],data=archive.getvalue())
    verify_remote_hashes(result.stdout,directory,hashes)
    if gws_source:
        for component, expected in release['pins'].items():
            actual = run_remote(['sudo', '-n', 'sha256sum', component]).stdout.decode().split()[0]
            if actual != expected:
                raise ValueError('GWS object component differs: '+component)
    launcher = release['launcher'] if gws_source else REMOTE+'/direct_object_gzip_launcher.py'
    args=['/usr/bin/python3',launcher,
          '--request',directory+'/request.json','--request-sha256',hashes['request.json'],
          '--continuity',directory+'/continuity.json','--continuity-sha256',hashes['continuity.json'],
          '--observation',directory+'/observation.json','--observation-sha256',hashes['observation.json']]
    (out/'launcher-args.json').write_text(json.dumps(args)+'\n')
    if release is not None:
        gws_object_release.record(out, release, args)
    unit='aurora-object-gzip-v2-admission-'+name.lower()
    run_remote(['sudo','-n','systemd-run','--unit='+unit,'--property=Type=exec',
                '--property=RemainAfterExit=yes','--property=RuntimeMaxSec=20min','--nice=15',*args])
    return {'out':str(out),'operation':operation,'admission_unit':unit+'.service',
            'transfer_started':False,'eviction_authorized':False,'retention_authorized':False}

if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('relative_path');args=parser.parse_args()
    print(json.dumps(prepare(args.relative_path),sort_keys=True))
