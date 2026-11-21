"""Recognize exact primary versions already assigned to the shared dispatcher.

Delegation prevents duplicate GWS-only admissions; it is never archive proof.
"""
from contextlib import closing
from pathlib import Path,PurePosixPath
import os,sqlite3,stat
ROOT=Path('/project/aurora/raw/rpgfmcw94')
DB=Path('/data/aurora/internal/archive_dispatch/queue.sqlite')


def delegated(candidates, *, root=ROOT, database=DB):
    root=Path(root);result=set()
    with closing(sqlite3.connect(Path(database).as_uri()+'?mode=ro',uri=True)) as db:
        for candidate in candidates:
            relative=candidate['relative_path'];parts=PurePosixPath(relative).parts
            if PurePosixPath(relative).is_absolute() or '..' in parts:
                raise ValueError('unsafe primary delegation path')
            path=root/relative
            try:
                before=path.lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1
                    or before.st_size!=candidate['size'] or before.st_mtime_ns//10**9!=candidate['mtime']
                    or any(p.is_symlink() for p in path.parents if p!=root and root in p.parents)):
                continue
            row=db.execute('SELECT size,mtime FROM delivery WHERE job=? AND relative_path=?',
                           ('raw','rpgfmcw94/'+relative)).fetchone()
            after=path.lstat()
            signature=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink)
            if row==(candidate['size'],candidate['mtime']) and signature(before)==signature(after):
                result.add(relative)
    return result


def _properties(unit):
    import subprocess
    raw=subprocess.run(['systemctl','show',unit,'-p','ActiveState','-p','SubState','-p','MainPID'],
                       check=True,capture_output=True,text=True,timeout=15).stdout
    return dict(line.split('=',1) for line in raw.splitlines() if '=' in line)


def _command(pid):
    return (Path('/proc')/str(pid)/'cmdline').read_bytes().rstrip(b'\0').split(b'\0')


def _children(pid):
    return (Path('/proc')/str(pid)/'task'/str(pid)/'children').read_text().split()


def _worker_running(unit,program,pin,*,wrapped=False):
    import hashlib
    props=_properties(unit)
    if props.get('ActiveState')!='active' or props.get('SubState')!='running':return False
    pid=props.get('MainPID','')
    if not pid.isdigit() or int(pid)<=0:raise ValueError('primary repair process is uncertain')
    if hashlib.sha256(program.read_bytes()).hexdigest()!=pin:
        raise ValueError('primary repair program changed')
    python=[b'/usr/bin/python3',str(program).encode()]
    expected=([b'/usr/bin/flock',b'-n',b'/run/lock/aurora-native-primary-repair.lock']+python) if wrapped else python
    if _command(pid)!=expected:raise ValueError('primary repair process differs')
    if wrapped:
        children=_children(pid)
        if len(children)!=1 or _command(children[0])!=python:
            raise ValueError('permanent primary repair child is uncertain')
    if _properties(unit)!=props:raise ValueError('primary repair changed during ownership observation')
    return True


def active_repair_owned(candidates,*,now=None):
    """Defer candidates within the exact live primary producers' admission scope.

    Both the dated worker and permanent flock-protected successor are
    authenticated. Deferral coordinates writers and is never delivery proof.
    """
    import re,time
    from datetime import date
    now=time.time() if now is None else now
    base=Path('/var/lib/aurora-deadline-repair-20261120')
    dated=_worker_running('aurora-deadline-native-primary-20261120.service',
        base/'restore_rolling_native.py','d9dea4920e877d7f5ee030e81d46b10af24708fa44abaa8de2fa8325f8658b21')
    permanent=_worker_running('aurora-native-primary-repair.service',
        base/'native_primary_repair.py','5f7d7b27f92aa5e1e4d91d46d12b8079a490ed942cbc94cd4460694955ef9f37',wrapped=True)
    if not permanent and not (dated and now<1795246200):return set()
    pattern=re.compile(r'Y(20[0-9]{2})/M([01][0-9])/D([0-3][0-9])/rpgfmcw94-aurora_([0-9]{6})_[0-9]{6}_P01_ZEN\.(?:LV0|LV1|LV0\.NC)')
    owned=set()
    for row in candidates:
        match=pattern.fullmatch(row['relative_path'])
        if not match or type(row.get('size')) is not int or not 0<row['size']<6*2**30:
            continue
        if type(row.get('mtime')) is not int or now-row['mtime']<2700:continue
        try:day=date(*map(int,match.group(1,2,3)))
        except ValueError:continue
        if day<date(2026,11,18) or match.group(4)!=day.strftime('%y%m%d'):continue
        if permanent or (dated and now<1795246200 and day<=date(2026,11,21)):
            owned.add(row['relative_path'])
    return owned
