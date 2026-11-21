#!/usr/bin/env python3
"""Pinned fence inspection and explicit reconciliation for one operation."""
import argparse,hashlib,json,os,pwd,re,stat,sys,types
from pathlib import Path
ROOT=Path('/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027')
BASE=ROOT/'aurora_radar_bounded_canary-v3.py'
OLD=ROOT/'landed-recovery-v1';DIRECT=ROOT/'landed-gws-v1';NEW=ROOT/'direct-staged-v1'
ORIGINAL=ROOT/'direct-staged-gzip-v5-vm01-lanes'/'direct_staged_launcher_gzip.py'
ORIGINAL_SHA='53f84c46dd02be01793f71de649d864c8bdc5a83d4d7091eee02815d999c3f2f'
BASE_SHA='71efc810dff1f03943b4de695b980b8701a077c95c44b6b10d9a6108e5d7fd3c'
OLD_LAUNCHER_SHA='12c56b2a7e0a988f667ff9d55472ec2c046f46c1a856357b631cc30855716c24'
FENCE=Path('/etc/aurora/gws-direct-publication.fence')

def bootstrap():
    raw = ORIGINAL.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=ORIGINAL_SHA: raise ValueError('launcher pin differs')
    mod=types.ModuleType('vm01_original');mod.__file__=str(ORIGINAL)
    exec(compile(raw,str(ORIGINAL),'exec'),mod.__dict__)
    base=mod.bootstrap();base.protected(ORIGINAL.parent)
    base.require(base.digest(base.control(ORIGINAL))==ORIGINAL_SHA,'protected launcher differs')
    return base,mod


def load_staged_component(original, base, name):
    if name == 'direct_stage_pipeline':
        return original.load(base, original.LONG/(name+'.py'), '45e81ded6b9296f20fc20724991d6156aa03dccd6115d314558e93bb074abffb', name)
    root=original.LONG if name=='gws_fence_cloud_adapter' else NEW
    return original.load(base, root/(name+'.py'), original.PINS[name+'.py'], name)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--proof',type=Path,required=True);p.add_argument('--proof-sha256',required=True)
    p.add_argument('--recovery-id',required=True);p.add_argument('--apply',action='store_true');a=p.parse_args()
    if os.geteuid()!=0:raise ValueError('run as root')
    if re.fullmatch('[a-z0-9][a-z0-9-]{2,63}',a.recovery_id) is None:raise ValueError('invalid recovery ID')
    base,original=bootstrap();load=original.load
    old=load(base,OLD/'landed_recovery_launcher.py',OLD_LAUNCHER_SHA,'fence_old_launcher')
    inspector=load(base,old.INSPECTOR,old.PINS[old.INSPECTOR],'fence_inspector')
    load(base,DIRECT/'gws_receive.py',original.DIRECT_PINS['gws_receive.py'],'gws_receive')
    for name in ('direct_stage_pipeline','gws_fence_recovery','gws_fence_cloud_adapter'):
        load_staged_component(original,base,name)
    base.protected(NEW);probe_raw=base.control(NEW/'remote_process_probe.py')
    base.require(hashlib.sha256(probe_raw).hexdigest()==original.PINS['remote_process_probe.py'],'process probe pin differs')
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
