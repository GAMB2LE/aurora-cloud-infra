"""Cloud adapter for one direct backlog transfer; not a production launcher.

The caller must pin this module and every dependency, use a root-protected
request and fresh continuity receipt, and run under an owned systemd worker.
The inherited controls retain the established locks and safety checks. This
adapter supplies no object verification, eviction or retention authority.
"""
import json
import os
import shlex
import subprocess
import time

from landed_gws_cloud_adapter import GWSCloudAdapter, GWS_PREFIX
import direct_capacity

IDENTITY = r'''import json,os,pathlib,stat,sys,time
p=pathlib.Path(sys.argv[1])
if not p.is_absolute() or '..' in p.parts or any(a.is_symlink() for a in (p,*p.parents)): raise RuntimeError('unsafe source path')
s=p.lstat()
if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or time.time()-s.st_mtime<21600: raise RuntimeError('source is not settled')
print(json.dumps([s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_uid,s.st_gid,s.st_mode,s.st_nlink]))
'''
READBACK = r'''import hashlib,json,os,pathlib,stat,sys
p=pathlib.Path(sys.argv[1]); size=int(sys.argv[2]); mtime=int(sys.argv[3]); expected=sys.argv[4]
if not p.is_absolute() or '..' in p.parts or any(a.is_symlink() for a in (p,*p.parents)): raise RuntimeError('unsafe destination path')
def sig(s): return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_uid,s.st_gid,s.st_mode,s.st_nlink]
b=p.lstat()
if not stat.S_ISREG(b.st_mode) or b.st_nlink!=1 or b.st_size!=size or b.st_mtime_ns!=mtime: raise RuntimeError('destination metadata differs')
h=hashlib.sha256();n=0
with os.fdopen(os.open(p,os.O_RDONLY|os.O_NOFOLLOW),'rb') as f:
 if sig(os.fstat(f.fileno()))!=sig(b): raise RuntimeError('destination changed')
 while chunk:=f.read(8*1024**2): n+=len(chunk); h.update(chunk)
 if sig(os.fstat(f.fileno()))!=sig(b): raise RuntimeError('destination changed during read')
if sig(p.lstat())!=sig(b) or n!=size or h.hexdigest()!=expected: raise RuntimeError('destination content differs')
print(json.dumps({'size':n,'mtime_ns':mtime,'sha256':h.hexdigest(),'stable':True,'identity':sig(b)}))
'''


class DirectCapacity:
    @staticmethod
    def capacity(inspector, size):
        return direct_capacity.check(size)


class BacklogCloudAdapter(GWSCloudAdapter):
    """Adds source and readback observations to the existing guarded adapter."""
    def __init__(self, base, inspector, helper, cfg, request_path, request_sha, continuity_path, continuity_sha):
        super().__init__(base, inspector, DirectCapacity(), helper, cfg, request_path,
                         request_sha, continuity_path, continuity_sha)
        self.deadline = None

    def set_deadline(self, deadline):
        self.deadline = deadline

    def _remaining(self, limit=120):
        if self.deadline is None: raise ValueError('operation deadline absent')
        value = min(limit, self.deadline-time.monotonic())
        if value <= 0: raise TimeoutError('operation deadline elapsed')
        return value

    def _field(self, code, *args, timeout=120):
        field = [*self.canary.ssh(), self.cfg['source_user']+'@'+self.cfg['source_host'],
                 shlex.join(['python3','-c',code,*map(str,args)])]
        result = self.inspector.run(self.inspector.service(field), timeout=self._remaining(timeout))
        if result.returncode or len(result.stdout)>4096: raise ValueError('field check failed')
        return json.loads(result.stdout)

    def _identity(self):
        identity = self._field(IDENTITY, self.cfg['source_path']+'/'+self.canary.name)
        if not isinstance(identity,list) or len(identity)!=9 or any(type(v) is not int or v<0 for v in identity):
            raise ValueError('invalid field identity')
        return identity

    def require_source_observation(self, observation):
        self.base.require(observation['relative_path']==self.canary.name, 'wrong observation path')
        identity = self._identity()
        self.base.require(identity[2]==observation['size'] and identity[3]//10**9==observation['mtime_seconds'],
                          'field differs from observation')

    def hash_settled_source(self, path, *, timeout):
        self.base.require(path==self.canary.name, 'wrong field path')
        result = self._field(self.base.source_code(), self.cfg['source_path']+'/'+path, timeout=timeout)
        self.base.require(isinstance(result,dict) and isinstance(result.get('identity'),list)
                          and len(result['identity'])==10 and result==self.request['expected_source'],
                          'field hash proof differs from pinned request')
        return {'relative_path':path, 'identity':result['identity'][:9], 'sha256':result['sha256']}

    def require_source_identity(self, source):
        self.base.require(source['relative_path']==self.canary.name and self._identity()==source['identity'],
                          'field identity changed')

    def require_local_absence(self, path):
        self.base.require(path==self.canary.name, 'wrong local path')
        target=self.base.safe(self.canary.root/path)
        self.base.require(not os.path.lexists(target), 'local file exists; reconcile first')

    def require_gws_absence_and_capacity(self, path, size):
        # A free export is not proof of group quota. Until a reviewed numeric
        # quota parser exists, admit only the explicit no-group-quota response.
        super().require_gws_absence_and_capacity(path, size)
        command=shlex.join(['quota','-s','-g','gws_gamb2le'])
        result=self.inspector.run(self.inspector.service([*self._gws_ssh(),command]), timeout=self._remaining())
        output=result.stdout.decode('utf-8',errors='strict').strip()
        if result.returncode or output != 'Disk quotas for group gws_gamb2le (gid 7051237): none':
            raise ValueError('GWS group quota is not conclusively unlimited')

    def independent_gws_readback(self, expected, *, timeout):
        self.base.require(expected['relative_path']==self.canary.name, 'wrong GWS path')
        path=GWS_PREFIX+'/'+self.canary.name
        remote=shlex.join(['python3','-c',READBACK,path,str(expected['size']),str(expected['mtime_ns']),expected['sha256']])
        result=self.inspector.run(self.inspector.service([*self._gws_ssh(),remote]), timeout=self._remaining(timeout))
        if result.returncode or len(result.stdout)>4096: raise ValueError('GWS readback failed')
        proof=json.loads(result.stdout); proof['relative_path']=self.canary.name
        return proof
