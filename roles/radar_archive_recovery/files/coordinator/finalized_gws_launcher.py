#!/usr/bin/env python3
"""Pinned one-file finalized-stage GWS publisher, guarded and non-authorizing."""
import argparse,hashlib,json,os,pwd,re,stat,subprocess,sys,time,types
from pathlib import Path
ROOT=Path('/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027')
LONG=ROOT/'direct-staged-gzip-v4-vm01'; FAST=ROOT/'direct-finalized-gws-v4'
BASE=ROOT/'aurora_radar_bounded_canary-v3.py'; OLD=ROOT/'landed-recovery-v1';DIRECT=ROOT/'landed-gws-v1';NEW=ROOT/'direct-staged-v1'
BASE_SHA='71efc810dff1f03943b4de695b980b8701a077c95c44b6b10d9a6108e5d7fd3c'
OLD_LAUNCHER_SHA='12c56b2a7e0a988f667ff9d55472ec2c046f46c1a856357b631cc30855716c24'
HELPER=Path('/usr/local/lib/aurora-gws-transfer')
HELPER_SHA='db8d5b671e91032ffec0afd9d62081969fcb6ce7d7fecee0d92fd630d2bfc987'
DIRECT_PINS={'gws_receive.py':'825a473de4c4e5c80ed18076f2463c7773a118d8ef37605874ef67a02ddfd132',
'landed_gws_cloud_adapter.py':'d66f621b16edbf6c0ebb2ec68ae9de10c060fce60827a31e68c1965e37be7edb'}
PINS={'direct_capacity.py': '616fa620cf0073354d61d0f401b2ef670cb33b0422eada19daf279f9bca5faad', 'direct_interrupt.py': 'bd750c83f22e3d49495a516de2a6c1d8187fb6122d6ddedf761d7e79593231a0', 'direct_stage_pipeline.py': '12fd17f6126c39481839ff671af891cea858a89d8c74876e477a12aa4d499487', 'direct_authorize.py': 'fc4270f3898d967e6812f46d6a52a2775f2ea3d51a556193de7ab75f322eeb0d', 'gws_publication_fence.py': 'fe17f96c00ff171ffe3bc4fae8383985920a4e88999b0c5bc6d06e1a6bd65b8c', 'gws_fence_recovery.py': '0e74818fc9d4d67c4ceb3e4686d11a3c8267a4876724e2d0a941cf4c61c1b59b', 'gws_fence_cloud_adapter.py': '8cfb1f8713eca2eeca609dcbdd9d1928cdad2fbc805e484386884b3be6fa8f13', 'direct_staged_commit.py': '50de388508bcd5059508bc9d63db9d3a280b0aebf95b0ca0b80a6a994d7190bd', 'direct_backlog_cloud_adapter.py': '57799109ef7a4a975848f0813a924bb18ef652574edcc2da3566316eb7119ca6', 'direct_staged_cloud_adapter.py': '59e64995d49da4cb462389c514dede7a6b57523dc4347f5800f3f64ee46b6450', 'direct_staged_recovery.py': '4a9a6187b33f610c40db5f65f73336b45da6b04afc2f9a1967f25b5a2aa34ade', 'gws_staged_receive.py': 'd15e9b110729b7e660e8931b960da195a1fe318fdb857734325f9452388a50ec', 'remote_commit_handshake.py': '27d65860538a80d94a405ab9e3aca1783e74414d22a2f8254db28547d441fe74', 'remote_process_probe.py': 'c91aafff9f879e9881de5f2f5f130431a87598b1e6abc7efdf1f1ce712a29b3c'}

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


def require_owned_service(base, operation):
    unit='aurora-direct-'+operation+'.service'
    base.require(re.fullmatch('[a-z0-9.-]+',unit) is not None,'invalid service name')
    raw=Path('/proc/self/cgroup').read_text()
    base.require('/'+unit in raw,'publisher must run inside its named service')
    result=subprocess.run(['/bin/systemctl','show',unit,'--property=MainPID','--value'],capture_output=True,text=True,timeout=15)
    base.require(result.returncode==0 and result.stdout.strip()==str(os.getpid()),'publisher is not service main process')
    return unit


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('request','continuity'):
        p.add_argument('--'+name,type=Path,required=True);p.add_argument('--'+name+'-sha256',required=True)
    p.add_argument('--apply',action='store_true');a=p.parse_args()
    if os.geteuid()!=0:raise ValueError('run as root')
    base=bootstrap();old=load(base,OLD/'landed_recovery_launcher.py',OLD_LAUNCHER_SHA,'staged_old_launcher')
    inspector=load(base,old.INSPECTOR,old.PINS[old.INSPECTOR],'staged_inspector')
    for name in ('landed_receipt_validation','landed_recovery_journal','landed_archive_absence','landed_cloud_adapter'):
        path=OLD/(name+'.py');load(base,path,old.PINS[path],name)
    for name in ('gws_receive','landed_gws_cloud_adapter'):
        load(base,DIRECT/(name+'.py'),DIRECT_PINS[name+'.py'],name)
    for name in (['direct_capacity', 'direct_interrupt', 'direct_stage_pipeline', 'direct_authorize', 'gws_publication_fence', 'gws_fence_recovery', 'gws_fence_cloud_adapter', 'direct_staged_commit', 'direct_backlog_cloud_adapter', 'direct_staged_cloud_adapter', 'direct_staged_recovery']):
        if name == 'direct_stage_pipeline':
            load(base,LONG/'direct_stage_pipeline.py','45e81ded6b9296f20fc20724991d6156aa03dccd6115d314558e93bb074abffb',name)
        elif name in ('gws_fence_cloud_adapter', 'direct_staged_cloud_adapter'):
            load(base,LONG/(name+'.py'),PINS[name+'.py'],name)
        else:
            load(base,NEW/(name+'.py'),PINS[name+'.py'],name)
    refresh=load(base,NEW/'refresh_staged_continuity.py','2f1c75a3f32d433402209e9d7307b25d88e9c643d7164167d6fa5f3406a4530a','refresh_staged_continuity')
    refreshed_core=load(base,LONG/'direct_staged_recovery_refresh.py','3e7804141a668e333569b29680e282f457da5028affad4d2a601af9866416c4b','direct_staged_recovery_refresh')
    base.protected(NEW)
    for name in ('gws_staged_receive.py','remote_commit_handshake.py','remote_process_probe.py'):
        base.require(base.digest(base.control(NEW/name))==PINS[name],'transport component differs')
    delivery=load(base,HELPER,HELPER_SHA,'installed_fenced_delivery')
    def read_pin(path,pin):
        base.protected(path.parent);raw=base.control(path);base.require(base.digest(raw)==pin,'input pin differs');return json.loads(raw)
    request=read_pin(a.request,a.request_sha256)
    operation=request['operation_id'];base.require(re.fullmatch('[a-z0-9][a-z0-9-]{1,63}',operation) is not None,'invalid operation ID')
    wrapper=base.control(base.WRAPPER);relocation=base.control(base.RELOCATION)
    base.require(base.digest(wrapper)==request['wrapper_sha256'] and base.digest(relocation)==request['relocation_helper_sha256'],'installed controls differ')
    helper=types.ModuleType('staged_relocation');helper.__file__=str(base.RELOCATION);exec(compile(relocation,str(base.RELOCATION),'exec'),helper.__dict__)
    cfg=base.config(wrapper);cfg['service_uid']=pwd.getpwnam('aurora').pw_uid
    probe_raw=base.control(NEW/'remote_process_probe.py')
    recovery_adapter=sys.modules['gws_fence_cloud_adapter'].FenceCloudAdapter(base,inspector,cfg['service_uid'],probe_raw)
    gzip_adapter=load(base,LONG/'gzip_gws_cloud_adapter.py','4ef9046b32144a1d6eb4779b8229be196b4002413cdff6c2566ac6f915985b55','gzip_gws_cloud_adapter')
    load(base,FAST/'finalized_expected.py','a11cfc0673b89049f938fa47f7d75af8061df321e498b355ec1f4665d0f63fb1','finalized_expected')
    finalized=load(base,FAST/'finalized_gws_adapter.py','6ef7c8ce481f2d6dcd56ef5a95081c3131139466ee8a5d86da8d33bc4eba0fab','finalized_gws_adapter')
    core=load(base,FAST/'direct_finalized_recovery.py','b70bf8ce666ee510bde61a8cf67cfb16b3524afd5d9a00fb5c1882a02a25377f','direct_finalized_recovery')
    lanes=load(base,FAST/'lane_recovery_locks.py','cf63f02838a82ae299976764f885f073eb3d41306877da955c99f4b41d92f322','lane_recovery_locks')
    class RefreshedAdapter(finalized.FinalizedStageMixin,gzip_adapter.GzipStagedCloudAdapter):
        def exclusive_recovery_locks(self):
            return lanes.locks(self.base,self.helper,self.cfg['lock_file'],'gws')
        def refresh_continuity(self,path,*,timeout):
            return refresh.refresh(self,NEW/'verify_released_radar_continuity.py','a956e6c556dbf9a57c85983d1878c92d4bbd8cd3b7508957245f18f2caea44d7',NEW/'released-radar-acceptance.json','f0edc78e2d99775dff9eff914f8eadd053e98b57f4e8346b8add8953d651f238',path,timeout=min(1800,timeout))
    adapter=RefreshedAdapter(base,inspector,helper,cfg,a.request,a.request_sha256,a.continuity,a.continuity_sha256,
        delivery_helper=delivery,fence_recovery_adapter=recovery_adapter)
    stage=request['finalized_stage']; expected=stage['expected']
    if (not sys.modules['finalized_expected'].valid_expected(expected)
            or request.get('relative_path')!=expected['relative_path']
            or request.get('expected_source',{}).get('sha256')!=expected['sha256']
            or request['expected_source']['identity'][2:4]!=[expected['size'],expected['mtime_ns']]):
        raise ValueError('finalized stage and pinned request differ')
    adapter.configure_finalized_stage(stage['operation'],stage['post_stage_operation'])
    journal=NEW/('transfer-'+operation+'.jsonl')
    if a.apply:
        unit=require_owned_service(base,operation)
        def check_during_stage():
            base.require(not os.path.lexists(base.HOLD), 'Relocation hold present')
            inspector.pinned(Path('/etc/aurora/ass-retention.hold'), inspector.RETENTION_SHA)
            base.require(not os.path.lexists('/etc/aurora/gws-direct-publication.fence'),
                         'Unresolved GWS publication fence')
            sys.modules['direct_capacity'].check(expected['size'])
        result=core.transfer_one(adapter,expected,journal,budget=7200,safety_check=check_during_stage)
        result['unit']=unit
    else:
        with sys.modules['direct_interrupt'].protect(),adapter.exclusive_recovery_locks():
            adapter.set_deadline(time.monotonic()+7200);adapter.require_controls_capacity_and_hold()
            handoff=adapter.authenticated_handoff(expected);adapter.require_local_absence(expected['relative_path'])
            adapter.require_gws_absence_and_capacity(expected['relative_path'],expected['size']);adapter.require_controls_capacity_and_hold()
            if adapter.authenticated_handoff(expected)!=handoff: raise ValueError('handoff changed during admission')
            adapter.finalized_stage_commands(expected,handoff,'0'*32)
            result={'mode':'read-only','expected':expected,'eviction_authorized':False,'retention_authorized':False,'transfer_started':False}
    print(json.dumps(result,sort_keys=True))

if __name__=='__main__':main()
