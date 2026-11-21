"""Attempt-bound selection for GWS-sourced object workers."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import re
import stat

ROOT='/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027'
OLD=ROOT+'/gws-object-source-v1/gws_object_launcher.py'
DIRECT=ROOT+'/direct-object-gzip-supervisor-v2/direct_object_gzip_launcher.py'
NEW_ROOT=ROOT+'/object-gws-vm01-20261110'
NEW=NEW_ROOT+'/gws_object_launcher.py'
LANE_ROOT=ROOT+'/object-gws-vm01-lanes-v1'
LANE=LANE_ROOT+'/gws_object_launcher.py'
LANE_PINS={LANE:'82036c541d9fd0860ce3da0112c094d48a626dde78808202eafb26109569f657',
           LANE_ROOT+'/gws_object_cloud_adapter.py':'1306e40df449d313b72196cf5130e0d6af187747eff705300e2607a94c140b65',
           LANE_ROOT+'/lane_recovery_locks.py':'cf63f02838a82ae299976764f885f073eb3d41306877da955c99f4b41d92f322'}
NEW_PINS={NEW:'b22552d20bd11ff6f4367f3afb3a870fd509d3f2b7ec1e76e40af61b6e181bb2',
          NEW_ROOT+'/gws_object_cloud_adapter.py':'1306e40df449d313b72196cf5130e0d6af187747eff705300e2607a94c140b65'}
OLD_PINS={OLD:'65df87e032734a309b2a99e73c10f94e898ba1eaa2dddca2329375317e64bb00',
          ROOT+'/gws-object-source-v1/gws_object_cloud_adapter.py':'3733230c5e5f982c5e7b0ea436550525a076f0ac75f2b3451546f68bde7e3483'}


def select():
    value=os.environ.get('RADAR_OBJECT_GWS_RELEASE','vm03')
    if value=='vm03':return {'id':'gws-object-vm03','launcher':OLD,'pins':OLD_PINS}
    if value=='vm01':return {'id':'gws-object-vm01','launcher':NEW,'pins':NEW_PINS}
    if value=='vm01-lanes':return {'id':'gws-object-vm01-lanes','launcher':LANE,'pins':LANE_PINS}
    raise ValueError('unknown GWS object worker release')


def record(out, release, argv):
    payload={'schema':'gws-object-worker-release-v1',**release,
             'argv_sha256':hashlib.sha256(json.dumps(argv,separators=(',',':')).encode()).hexdigest()}
    p=Path(out)/'object-worker-release.json'
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(payload,f,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
    fd=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def resolve(out):
    out=Path(out);argv=json.loads((out/'launcher-args.json').read_bytes())
    if not isinstance(argv,list) or len(argv)!=14 or argv[0]!='/usr/bin/python3':
        raise ValueError('invalid object launcher arguments')
    if argv[1] not in (OLD,NEW,LANE,DIRECT):raise ValueError('unknown object launcher')
    match=re.fullmatch(r'direct-object-gzip-preparation-(20\d{6}T(?:\d{4}|\d{6})Z)',out.name)
    if not match:raise ValueError('invalid object preparation name')
    stamp=match[1].lower()
    directory=ROOT+'/direct-object-gzip-supervisor-v2/input-'+stamp
    inputs={}
    for offset,name in ((2,'request'),(6,'continuity'),(10,'observation')):
        digest=argv[offset+3]
        if (argv[offset]!='--'+name or argv[offset+1]!=directory+'/'+name+'.json'
                or argv[offset+2]!='--'+name+'-sha256'
                or not isinstance(digest,str) or re.fullmatch('[0-9a-f]{64}',digest) is None):
            raise ValueError('object launcher inputs differ')
        fd=os.open(out/(name+'.json'),os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise ValueError('unsafe object input')
            raw=f.read()
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('object input hash differs')
        inputs[name]=raw
    request=json.loads(inputs['request'])
    short=stamp[9:]
    if request.get('operation_id') not in ('20261027-gzip-next-'+stamp+'-0',
                                           '20261027-gzip-next-'+short+'-0'):
        raise ValueError('object operation differs from preparation')
    path=out/'object-worker-release.json'
    if os.path.lexists(path):
        if path.is_symlink():raise ValueError('unsafe object release record')
        value=json.loads(path.read_bytes())
        expected = ({'id':'gws-object-vm01-lanes','launcher':LANE,'pins':LANE_PINS} if argv[1]==LANE else
                    {'id':'gws-object-vm01','launcher':NEW,'pins':NEW_PINS} if argv[1]==NEW else
                    {'id':'gws-object-vm03','launcher':OLD,'pins':OLD_PINS} if argv[1]==OLD else None)
        if expected is None or value!={'schema':'gws-object-worker-release-v1',**expected,
           'argv_sha256':hashlib.sha256(json.dumps(argv,separators=(',',':')).encode()).hexdigest()}:
            raise ValueError('object release record differs')
        return expected,argv
    if argv[1] in (NEW,LANE):raise ValueError('vm01 object release record missing')
    return ({'id':'legacy-object','launcher':argv[1],'pins':{}},argv)


def verify_pins(release,run_remote):
    for path,pin in release['pins'].items():
        fields=run_remote(['sudo','-n','sha256sum',path],timeout=60).stdout.decode().split()
        if fields != [pin,path]:raise ValueError('object worker component differs: '+path)


def authenticate_worker(unit,argv,run_remote):
    output=run_remote(['systemctl','show',unit,'-p','ExecStart','--value']).stdout.decode()
    if 'argv[]='+shlex.join([*argv,'--apply'])+' ;' not in output:
        raise ValueError('recorded object worker command differs')
