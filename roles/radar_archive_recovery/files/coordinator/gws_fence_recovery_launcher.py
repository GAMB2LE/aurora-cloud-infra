#!/usr/bin/env python3
"""Pinned fence inspection and explicit reconciliation for one operation."""
import argparse,hashlib,json,os,pwd,re,stat,sys,types
from pathlib import Path
ROOT=Path('/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027')
BASE=ROOT/'aurora_radar_bounded_canary-v3.py'
OLD=ROOT/'landed-recovery-v1';DIRECT=ROOT/'landed-gws-v1';NEW=ROOT/'direct-fence-v1'
BASE_SHA='71efc810dff1f03943b4de695b980b8701a077c95c44b6b10d9a6108e5d7fd3c'
OLD_LAUNCHER_SHA='12c56b2a7e0a988f667ff9d55472ec2c046f46c1a856357b631cc30855716c24'
FENCE=Path('/etc/aurora/gws-direct-publication.fence')
PINS={'gws_receive.py':'825a473de4c4e5c80ed18076f2463c7773a118d8ef37605874ef67a02ddfd132',
      'direct_stage_pipeline.py':'12fd17f6126c39481839ff671af891cea858a89d8c74876e477a12aa4d499487',
      'remote_process_probe.py':'c91aafff9f879e9881de5f2f5f130431a87598b1e6abc7efdf1f1ce712a29b3c',
      'gws_fence_recovery.py':'d1b9b8c7eb77bc0946c00cf87cffa0925f332a74ac220d04cd1f2883416a8b30',
      'gws_fence_cloud_adapter.py':'1b9b6324098e757364924400a1ba93248c2fa904fa885dd0dad5a382a97e9797'}

def bootstrap():
    for p in (BASE,*BASE.parents):
        s=p.lstat()
        if s.st_uid!=0 or s.st_mode&0o022 or stat.S_ISLNK(s.st_mode): raise ValueError('unsafe base path')
    before=BASE.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: raise ValueError('unsafe base file')
    fd=os.open(BASE,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        opened=os.fstat(fd);chunks=[]
        while chunk:=os.read(fd,1024*1024):chunks.append(chunk)
        after=os.fstat(fd)
    finally:os.close(fd)
    raw=b''.join(chunks); sig=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode,s.st_uid,s.st_nlink)
    if sig(before)!=sig(opened) or sig(before)!=sig(after) or sig(before)!=sig(BASE.lstat()) or hashlib.sha256(raw).hexdigest()!=BASE_SHA: raise ValueError('base pin differs')
    mod=types.ModuleType('direct_rehearsal_base');mod.__file__=str(BASE);exec(compile(raw,str(BASE),'exec'),mod.__dict__);return mod


def load(base,path,pin,name):
    base.protected(path.parent);raw=base.control(path)
    base.require(hashlib.sha256(raw).hexdigest()==pin,'component pin differs: '+name)
    module=types.ModuleType(name);module.__file__=str(path);sys.modules[name]=module
    exec(compile(raw,str(path),'exec'),module.__dict__);return module


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--proof',type=Path,required=True);p.add_argument('--proof-sha256',required=True)
    p.add_argument('--recovery-id',required=True);p.add_argument('--apply',action='store_true');a=p.parse_args()
    if os.geteuid()!=0:raise ValueError('run as root')
    if re.fullmatch('[a-z0-9][a-z0-9-]{2,63}',a.recovery_id) is None:raise ValueError('invalid recovery ID')
    base=bootstrap();old=load(base,OLD/'landed_recovery_launcher.py',OLD_LAUNCHER_SHA,'fence_old_launcher')
    inspector=load(base,old.INSPECTOR,old.PINS[old.INSPECTOR],'fence_inspector')
    load(base,DIRECT/'gws_receive.py',PINS['gws_receive.py'],'gws_receive')
    for name in ('direct_stage_pipeline','gws_fence_recovery','gws_fence_cloud_adapter'):
        load(base,NEW/(name+'.py'),PINS[name+'.py'],name)
    base.protected(NEW);probe_raw=base.control(NEW/'remote_process_probe.py')
    base.require(hashlib.sha256(probe_raw).hexdigest()==PINS['remote_process_probe.py'],'process probe pin differs')
    base.protected(a.proof.parent);raw=base.control(a.proof)
    base.require(base.digest(raw)==a.proof_sha256,'input proof pin differs')
    proof=json.loads(raw);adapter=sys.modules['gws_fence_cloud_adapter'].FenceCloudAdapter(base,inspector,pwd.getpwnam('aurora').pw_uid,probe_raw)
    recovery=sys.modules['gws_fence_recovery'];receipt=NEW/('reconciliation-'+a.recovery_id+'.json')
    base.require(not os.path.lexists(receipt),'receipt already exists; use recovery evidence')
    if a.apply:
        value=recovery.reconcile(adapter,FENCE,receipt,proof)
        result={'mode':'applied','receipt':str(receipt),'outcome':value['outcome'],
                'eviction_authorized':False,'retention_authorized':False}
    else:
        with adapter.recovery_delivery_lock():
            marker,_,value=recovery._read_marker(FENCE,0);ready=value['ready']
            canonical=hashlib.sha256(json.dumps(proof,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            base.require(canonical==ready['proof_sha256'],'marker proof differs')
            stage=proof['stage'];expected={k:stage[k] for k in ('relative_path','size','mtime_ns','sha256')}
            state=adapter.remote_process_state(ready['identity'])
            outcome=adapter.read_publication_outcome(expected,stage);recovery._check_outcome(outcome,expected)
            result={'mode':'read-only','marker_sha256':base.digest(marker),'remote_state':state,'outcome':outcome,
                    'eviction_authorized':False,'retention_authorized':False}
    print(json.dumps(result,sort_keys=True))

if __name__=='__main__':main()
