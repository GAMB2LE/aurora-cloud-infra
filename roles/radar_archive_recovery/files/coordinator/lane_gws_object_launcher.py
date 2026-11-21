#!/usr/bin/env python3
"""Pinned provisional direct-to-object version copy; no release authority."""
import argparse,hashlib,json,os,pwd,re,secrets,stat,subprocess,sys,time,types
from pathlib import Path
ROOT=Path('/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027')
BASE=ROOT/'aurora_radar_bounded_canary-v3.py';OLD=ROOT/'landed-recovery-v1';DIRECT=ROOT/'landed-gws-v1';DUAL=ROOT/'landed-dual-v1';NEW=ROOT/'direct-object-v1'
BASE_SHA='71efc810dff1f03943b4de695b980b8701a077c95c44b6b10d9a6108e5d7fd3c'
OLD_LAUNCHER_SHA='12c56b2a7e0a988f667ff9d55472ec2c046f46c1a856357b631cc30855716c24'
DIRECT_PINS={'gws_receive.py':'825a473de4c4e5c80ed18076f2463c7773a118d8ef37605874ef67a02ddfd132',
'landed_gws_cloud_adapter.py':'d66f621b16edbf6c0ebb2ec68ae9de10c060fce60827a31e68c1965e37be7edb'}
OBJECT_READER_SHA='bbbb2b84c7d988ca6aec61a56afbe359b9f8fe75916ee07a62d0d286204ff50b'
PINS={'direct_capacity.py': '616fa620cf0073354d61d0f401b2ef670cb33b0422eada19daf279f9bca5faad', 'direct_interrupt.py': 'bd750c83f22e3d49495a516de2a6c1d8187fb6122d6ddedf761d7e79593231a0', 'direct_pipeline_bounded.py': '91af3050c0ea47c6483cd2e00087c45e20c2f17db5e4045d58277a3e92003e6d', 'direct_backlog_recovery.py': '83ee53d03eeac403275d2eaeda9b88ecf9d29e904188cef1dcd909e56c8ca2cb', 'direct_backlog_cloud_adapter.py': '57799109ef7a4a975848f0813a924bb18ef652574edcc2da3566316eb7119ca6', 'direct_object_pipeline.py': '30a6110291a4a0d10b2a12e6b16a4e0f84d3c213f1306e4a77f1c5aca147f5f3', 'direct_object_cloud_adapter.py': '17aae63f1a8431334fdbab50c6584396c30f680a10b0130f3537e9f98682cd46', 'direct_object_recovery.py': '71ce21b87b1555df3539c8fc8cced5a4e872abcd99c987e71a5b0314fe461a6e'}

LONG=ROOT/'direct-object-long-v1'
LONG_PINS={'direct_object_long_pipeline.py': 'f1592f68d3ccba0cba2170e132180ed2fdeedf341b2b2f0cb2c4bcf2905f51d3', 'direct_object_long_cloud_adapter.py': '7dd6ffc1d38573d6f6756b5cb0bcdb1e6e35e1017e92eb5187f0ab91ce83805b', 'direct_object_long_recovery.py': '547a340484a5f121eb8ff8988a01a0dc03546b6bfdf2ba812a54daaffc721942'}

GZIP=ROOT/'direct-object-gzip-supervisor-v2'
GZIP_PINS={'direct_object_gzip_pipeline.py': '07e076bbee03d8d9532e56a6d8eb75aeadea2d30e57bed4298c011f04277b3a4', 'direct_object_gzip_cloud_adapter.py': '28ea024838b9ace925833269c2cf283210166a3bfa903e8c0a8eab0cdbe64e7e', 'direct_object_gzip_recovery.py': '732617202df9b82d3a84fac413e3bc7e752206b39bcbb78019ac290cfb99e791'}

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


def require_owned_service(base,operation):
    unit='aurora-object-direct-'+operation+'.service'
    base.require(re.fullmatch('[a-z0-9.-]+',unit) is not None,'invalid service name')
    base.require('/'+unit in Path('/proc/self/cgroup').read_text(),'must run in named service')
    result=subprocess.run(['/bin/systemctl','show',unit,'--property=MainPID','--value'],capture_output=True,text=True,timeout=15)
    base.require(result.returncode==0 and result.stdout.strip()==str(os.getpid()),'publisher is not service main process')
    return unit


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('request','continuity','observation'):
        p.add_argument('--'+name,type=Path,required=True);p.add_argument('--'+name+'-sha256',required=True)
    p.add_argument('--apply',action='store_true');a=p.parse_args()
    if os.geteuid()!=0:raise ValueError('run as root')
    base=bootstrap();old=load(base,OLD/'landed_recovery_launcher.py',OLD_LAUNCHER_SHA,'object_old_launcher')
    inspector=load(base,old.INSPECTOR,old.PINS[old.INSPECTOR],'object_inspector')
    for name in ('landed_receipt_validation','landed_recovery_journal','landed_archive_absence','landed_cloud_adapter'):
        path=OLD/(name+'.py');load(base,path,old.PINS[path],name)
    for name in ('gws_receive','landed_gws_cloud_adapter'):
        load(base,DIRECT/(name+'.py'),DIRECT_PINS[name+'.py'],name)
    load(base,DUAL/'landed_object_read.py',OBJECT_READER_SHA,'landed_object_read')
    for name in (['direct_capacity', 'direct_interrupt', 'direct_pipeline_bounded', 'direct_backlog_recovery', 'direct_backlog_cloud_adapter', 'direct_object_pipeline', 'direct_object_cloud_adapter', 'direct_object_recovery']):
        load(base,NEW/(name+'.py'),PINS[name+'.py'],name)
    for name in ('direct_object_long_pipeline','direct_object_long_cloud_adapter','direct_object_long_recovery'):
        load(base,LONG/(name+'.py'),LONG_PINS[name+'.py'],name)
    for name in ('direct_object_gzip_pipeline','direct_object_gzip_cloud_adapter','direct_object_gzip_recovery'):
        load(base,GZIP/(name+'.py'),GZIP_PINS[name+'.py'],name)
    def read_pin(path,pin):
        base.protected(path.parent);raw=base.control(path);base.require(base.digest(raw)==pin,'input pin differs');return json.loads(raw)
    request=read_pin(a.request,a.request_sha256);observation=read_pin(a.observation,a.observation_sha256)
    operation=request['operation_id'];base.require(re.fullmatch('[a-z0-9][a-z0-9-]{1,63}',operation) is not None,'invalid operation ID')
    wrapper=base.control(base.WRAPPER);relocation=base.control(base.RELOCATION)
    base.require(base.digest(wrapper)==request['wrapper_sha256'] and base.digest(relocation)==request['relocation_helper_sha256'],'installed controls differ')
    helper=types.ModuleType('object_relocation');helper.__file__=str(base.RELOCATION);exec(compile(relocation,str(base.RELOCATION),'exec'),helper.__dict__)
    cfg=base.config(wrapper);cfg['service_uid']=pwd.getpwnam('aurora').pw_uid
    gws_adapter = load(base, Path(__file__).parent/'gws_object_cloud_adapter.py',
                       '1306e40df449d313b72196cf5130e0d6af187747eff705300e2607a94c140b65', 'gws_object_cloud_adapter')
    lanes = load(base, Path(__file__).parent/'lane_recovery_locks.py',
                 'cf63f02838a82ae299976764f885f073eb3d41306877da955c99f4b41d92f322', 'lane_recovery_locks')
    class LaneObjectAdapter(gws_adapter.GWSObjectCloudAdapter):
        def exclusive_recovery_locks(self):
            return lanes.locks(self.base, self.helper, self.cfg['lock_file'], 'object')
    adapter = LaneObjectAdapter(
        base, inspector, helper, cfg, a.request, a.request_sha256, a.continuity, a.continuity_sha256
    )
    core=sys.modules['direct_object_gzip_recovery'];shared=sys.modules['direct_backlog_recovery'];journal=NEW/('transfer-'+operation+'.jsonl')
    if a.apply:
        unit=require_owned_service(base,operation);result=core.transfer_one(adapter,observation,journal);result['unit']=unit
    else:
        shared._validate_observation(observation,time.time())
        with sys.modules['direct_interrupt'].protect(),adapter.exclusive_recovery_locks():
            adapter.set_deadline(time.monotonic()+7200);adapter.require_controls_capacity_and_hold();adapter.require_source_observation(observation)
            source=adapter.hash_settled_source(observation['relative_path'],timeout=1800);shared._validate_source(observation,source)
            shared._validate_observation(observation,time.time());adapter.require_source_observation(observation);adapter.require_source_identity(source)
            expected={'relative_path':observation['relative_path'],'size':observation['size'],'mtime_ns':source['identity'][3],'sha256':source['sha256']}
            key=sys.modules['direct_object_pipeline'].OBJECT_BASE+'/'+secrets.token_hex(16)+'/'+source['sha256']+'.radar'
            absence=adapter.require_exact_object_absence(key,timeout=180);adapter.require_controls_capacity_and_hold();adapter.require_source_identity(source)
            adapter.object_commands(expected,source['identity'],key)
            result={'mode':'read-only','expected':expected,'object_head_control':absence['positive_control'],'candidate_absent':absence['absent'],
                    'eviction_authorized':False,'retention_authorized':False,'transfer_started':False}
    print(json.dumps(result,sort_keys=True))

if __name__=='__main__':main()
