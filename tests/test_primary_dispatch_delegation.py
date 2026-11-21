import os,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'roles/radar_archive_recovery/files/coordinator'))
from primary_dispatch_delegation import delegated


def test_only_exact_existing_queued_primary_is_delegated(tmp_path):
    root=tmp_path/'raw';root.mkdir();p=root/'one.LV1';p.write_bytes(b'correct')
    os.utime(p,ns=(100000000000,100000000000))
    database=tmp_path/'q.sqlite'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE delivery(job,relative_path,size,mtime)')
        db.execute('INSERT INTO delivery VALUES (?,?,?,?)',('raw','rpgfmcw94/one.LV1',7,100))
    candidate={'relative_path':'one.LV1','size':7,'mtime':100}
    assert delegated([candidate],root=root,database=database)=={'one.LV1'}
    assert delegated([dict(candidate,size=8)],root=root,database=database)==set()
    p.write_bytes(b'changed bytes')
    assert delegated([candidate],root=root,database=database)==set()


def test_unqueued_or_symlinked_primary_is_not_delegated(tmp_path):
    root=tmp_path/'raw';root.mkdir();p=root/'one.LV1';p.write_bytes(b'correct')
    os.utime(p,ns=(100000000000,100000000000))
    database=tmp_path/'q.sqlite'
    with sqlite3.connect(database) as db:db.execute('CREATE TABLE delivery(job,relative_path,size,mtime)')
    assert not delegated([{'relative_path':'one.LV1','size':7,'mtime':100}],root=root,database=database)
    (root/'link.LV1').symlink_to(p)
    with sqlite3.connect(database) as db:db.execute('INSERT INTO delivery VALUES (?,?,?,?)',('raw','rpgfmcw94/link.LV1',7,100))
    assert not delegated([{'relative_path':'link.LV1','size':7,'mtime':100}],root=root,database=database)


def test_permanent_ownership_matches_producer_scope(monkeypatch):
    import primary_dispatch_delegation as module
    monkeypatch.setattr(module,'_worker_running',lambda unit,*args,**kwargs:unit=='aurora-native-primary-repair.service')
    now=1795300000
    def row(day,stamp,size=100):return {'relative_path':f'Y2026/M11/D{day}/rpgfmcw94-aurora_{stamp}_005959_P01_ZEN.LV0.NC','size':size,'mtime':now-22000}
    good=row('22','261122')
    candidates=[good,row('22','261121'),row('31','261131'),row('17','261117'),row('22','261122',6*2**30),dict(good,mtime=now-60)]
    assert module.active_repair_owned(candidates,now=now)=={good['relative_path']}


def test_dated_owner_stops_at_boundary_and_permanent_continues(monkeypatch):
    import primary_dispatch_delegation as module
    monkeypatch.setattr(module,'_worker_running',lambda unit,*args,**kwargs:unit=='aurora-deadline-native-primary-20261120.service')
    row={'relative_path':'Y2026/M11/D20/rpgfmcw94-aurora_261120_005959_P01_ZEN.LV1','size':100,'mtime':1795200000}
    assert module.active_repair_owned([row],now=1795246199)=={row['relative_path']}
    assert module.active_repair_owned([row],now=1795246200)==set()


def test_flock_owner_requires_exact_program_child_and_stable_unit(tmp_path,monkeypatch):
    import hashlib,pytest
    import primary_dispatch_delegation as module
    program=tmp_path/'repair.py';program.write_bytes(b'known program')
    pin=hashlib.sha256(program.read_bytes()).hexdigest()
    props={'ActiveState':'active','SubState':'running','MainPID':'123'}
    monkeypatch.setattr(module,'_properties',lambda unit:props)
    python=[b'/usr/bin/python3',str(program).encode()]
    commands={'123':[b'/usr/bin/flock',b'-n',b'/run/lock/aurora-native-primary-repair.lock',*python],'124':python}
    monkeypatch.setattr(module,'_command',lambda pid:commands[pid]);monkeypatch.setattr(module,'_children',lambda pid:['124'])
    assert module._worker_running('unit',program,pin,wrapped=True)
    commands['124']=[b'other']
    with pytest.raises(ValueError):module._worker_running('unit',program,pin,wrapped=True)
    commands['124']=python;program.write_bytes(b'changed')
    with pytest.raises(ValueError):module._worker_running('unit',program,pin,wrapped=True)
