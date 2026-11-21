"""Use a root-pinned finalized ASS stage with the existing fenced GWS adapter.

This mixin supplies transport and handoff authentication only. Its launcher
must independently pin this code, hold all existing locks, and reconcile any
uncertain worker before retrying.
"""
import json
import re
import shlex
import stat

from finalized_expected import valid_expected

RELEASE = '/var/lib/aurora-radar-finalized-sender-v2'
PINS = {
    'inspect_protected_stage.py': 'cdc709e0c34d68c61d1bb6544d93ea3d71beaf68185399a6f1079a7a432739f8',
    'read_stage_handoff.py': '82b51068e7b254ad6d585b1a87a59c219e53813abbba6dfb1527f691a2139beb',
    'stream_protected_stage.py': 'fc28b8be8f6ea751e8f3cc6edb04c7fe7076f132d51f78b99716607055c471c3',
    'send_finalized_stage.py': '2456b3816b034fa15c1ce3ba435a72817edc13dd5f598fecdb97366404023d3a',
}

# Executed over the existing authenticated SSH connection as aurora. Load only
# the checked bytes, never a writable module path or pre-existing bytecode.
BOOTSTRAP = r'''import hashlib,json,os,stat,sys,types
root='/var/lib/aurora-radar-finalized-sender-v2'
pins=json.loads(sys.argv[1]); action=sys.argv[2]; op=sys.argv[3]; after=sys.argv[4]
for path in ('/','/var','/var/lib',root):
 s=os.lstat(path)
 if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022: raise ValueError('unsafe release directory')
raws={}
for name,digest in pins.items():
 if '/' in name or name in ('.','..'): raise ValueError('invalid release pin')
 path=root+'/'+name; fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(fd,'rb') as f:
  a=os.fstat(f.fileno()); raw=f.read(1048577); b=os.fstat(f.fileno())
 c=os.stat(path,follow_symlinks=False)
 sig=lambda x:(x.st_dev,x.st_ino,x.st_size,x.st_mtime_ns,x.st_ctime_ns,x.st_mode,x.st_uid,x.st_nlink)
 if (not stat.S_ISREG(a.st_mode) or a.st_uid!=0 or a.st_mode&0o022 or a.st_nlink!=1 or a.st_size>1048576
     or sig(a)!=sig(b) or sig(a)!=sig(c) or hashlib.sha256(raw).hexdigest()!=digest): raise ValueError('release pin differs')
 raws[name]=raw
for name in ('inspect_protected_stage','read_stage_handoff','stream_protected_stage','send_finalized_stage'):
 m=types.ModuleType(name);m.__file__=root+'/'+name+'.py';sys.modules[name]=m
 exec(compile(raws[name+'.py'],m.__file__,'exec'),m.__dict__)
if action=='inspect':
 print(json.dumps(sys.modules['read_stage_handoff'].read('/var/lib/aurora-radar-fast-staging',op,after),sort_keys=True))
elif action=='send':
 expected=json.loads(sys.argv[5]); r=sys.modules['send_finalized_stage'].send('/var/lib/aurora-radar-fast-staging',op,after,expected,sys.stdout.buffer)
 print(json.dumps(r,sort_keys=True),file=sys.stderr,flush=True)
else: raise ValueError('unknown sender action')
'''


class FinalizedStageMixin:
    def configure_finalized_stage(self, operation, after):
        if any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value) for value in (operation, after)):
            raise ValueError('invalid finalized-stage operation')
        self.stage_operation, self.post_stage_operation = operation, after

    def authenticated_handoff(self, expected):
        if not valid_expected(expected): raise ValueError('invalid expected version')
        handoff = self._field(BOOTSTRAP, json.dumps(PINS, sort_keys=True), 'inspect',
                              self.stage_operation, self.post_stage_operation)
        actual = {'relative_path': handoff.get('source'), 'size': handoff.get('size'),
                  'mtime_ns': handoff.get('source_mtime_ns'), 'sha256': handoff.get('sha256')}
        if actual != expected or handoff.get('stage_operation') != self.stage_operation \
                or handoff.get('post_stage_operation') != self.post_stage_operation:
            raise ValueError('root handoff differs from exact admission version')
        return handoff

    def finalized_stage_commands(self, expected, handoff, nonce):
        if (not valid_expected(expected) or not re.fullmatch('[0-9a-f]{32}', nonce)
                or self.authenticated_handoff(expected) != handoff):
            raise ValueError('handoff or destination operation differs')
        # The inherited method supplies the established pinned gzip receiver.
        # Its original source command is constructed but never executed.
        synthetic = [0, 0, expected['size'], expected['mtime_ns'], 0, 0, 0, stat.S_IFREG | 0o440, 1]
        sender, receiver, _, receiver_raw = super().staged_direct_commands(expected, synthetic, nonce)
        args = ['nice', '-n', '15', 'python3', '-c', BOOTSTRAP, json.dumps(PINS, sort_keys=True),
                'send', self.stage_operation, self.post_stage_operation, json.dumps(expected, sort_keys=True)]
        return sender[:-1] + [shlex.join(args)], receiver, (BOOTSTRAP + json.dumps(PINS, sort_keys=True)).encode(), receiver_raw
