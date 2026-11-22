#!/usr/bin/env python3
"""Change only WXcam eligibility, preserving the live ASFS guard and its pin."""
import ast
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

CANONICAL = Path('/usr/local/bin/aurora-ass-retention')
ORIGINAL = Path('/usr/local/lib/aurora-asfs-storage-originals-20261110/aurora-ass-retention')
BASE = Path('/var/lib/aurora-retention-return-20261121')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def change(raw, paused):
    text = raw.decode(); tree = ast.parse(text)
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'STREAMS' for t in n.targets))
    payload = ast.literal_eval(node.value.args[0]); rows = json.loads(payload)
    selected = [r for r in rows if r['name'] == 'wxcam']
    if len(selected) != 1 or type(selected[0]['prune_enabled']) is not bool:
        raise ValueError('unknown WXcam policy contract')
    if selected[0]['prune_enabled'] is (not paused):
        return raw
    selected[0]['prune_enabled'] = not paused
    if text.count(payload) != 1:
        raise ValueError('ambiguous stream payload')
    revised = text.replace(payload, json.dumps(rows, sort_keys=True), 1).encode()
    compile(revised, str(CANONICAL), 'exec')
    return revised


def read(path):
    info = path.lstat()
    if path.resolve() != path or not stat.S_ISREG(info.st_mode) or info.st_uid or info.st_mode & 0o022:
        raise ValueError('unsafe coordinator control')
    return path.read_bytes(), {'uid':info.st_uid, 'gid':info.st_gid, 'mode':stat.S_IMODE(info.st_mode)}


def atomic(path, raw, meta):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.retention-policy-')
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw); os.fchown(out.fileno(),meta['uid'],meta['gid']);os.fchmod(out.fileno(),meta['mode']);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
        fd=os.open(path.parent,os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.exists(name):os.unlink(name)


def apply(paused=True):
    state=subprocess.check_output(['systemctl','show','aurora-ass-retention-window.service','aurora-ass-retention.service','-p','ActiveState','--value'],text=True).split()
    if any(s not in ('inactive','failed') for s in state):
        raise BlockingIOError('leave active retention workers unchanged')
    raw, meta=read(CANONICAL); after=change(raw,paused); changes=[]
    marker=b'run_python_original'
    if marker in raw:
        original, metadata=read(ORIGINAL); pin=sha(original)
        exact=("'/usr/local/bin/aurora-ass-retention', '"+pin+"'").encode()
        if raw.count(exact)!=1:
            raise ValueError('ASFS wrapper does not pin the preserved entrypoint')
        revised=change(original,paused)
        after=after.replace(exact,("'/usr/local/bin/aurora-ass-retention', '"+sha(revised)+"'").encode(),1)
        changes.append((ORIGINAL,original,revised,metadata))
    changes.append((CANONICAL,raw,after,meta))
    if all(a==b for _,a,b,_ in changes):
        print(json.dumps({'wxcam_prune_enabled':not paused,'changed':False}));return
    folder=BASE/('source-policy-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'));folder.mkdir(mode=0o700)
    for i,(path,before,_,_) in enumerate(changes):
        atomic(folder/(str(i)+'-before'),before,{'uid':0,'gid':0,'mode':0o400})
    try:
        for path,before,after,metadata in changes:
            if read(path)[0]!=before:raise ValueError('another writer changed source policy')
            atomic(path,after,metadata)
        for path,_,after,_ in changes:
            if read(path)[0]!=after:raise ValueError('installed source policy differs')
    except BaseException:
        for path,before,after,metadata in reversed(changes):
            if read(path)[0] not in (before,after):raise ValueError('third party edit; do not overwrite')
            atomic(path,before,metadata)
        raise
    result={'wxcam_prune_enabled':not paused,'changed':True,'asfs_guard_preserved':marker in raw,
            'components':[{'path':str(p),'before_sha256':sha(a),'after_sha256':sha(b)} for p,a,b,_ in changes]}
    atomic(folder/'result.json',(json.dumps(result,sort_keys=True)+'\n').encode(),{'uid':0,'gid':0,'mode':0o400})
    print(json.dumps(result))


if __name__=='__main__':
    if os.geteuid()!=0:raise PermissionError('root policy control required')
    apply()
