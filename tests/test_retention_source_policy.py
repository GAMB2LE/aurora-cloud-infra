import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SOURCE=Path(__file__).parents[1]/'roles/ass_retention/files/set-retention-stream-policy.py'


def load():
    spec=importlib.util.spec_from_file_location('policy',SOURCE)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


@pytest.fixture
def policy(tmp_path,monkeypatch):
    m=load();m.BASE=tmp_path;m.CANONICAL=tmp_path/'coordinator';m.ORIGINAL=tmp_path/'preserved'
    original=("import json\nSTREAMS=json.loads(r'''"+json.dumps([
        {'name':'radar','prune_enabled':True},{'name':'wxcam','prune_enabled':True}])+"''')\nmain()\n").encode()
    pin=hashlib.sha256(original).hexdigest()
    wrapper=original.replace(b'main()\n',("guard.run_python_original('/usr/local/bin/aurora-ass-retention', '"+pin+"', argv)\n").encode())
    m.ORIGINAL.write_bytes(original);m.CANONICAL.write_bytes(wrapper)
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**kw:'inactive\ninactive\n')
    monkeypatch.setattr(m,'read',lambda p:(p.read_bytes(),{}))
    monkeypatch.setattr(m,'atomic',lambda p,r,meta:p.write_bytes(r))
    return m,original,wrapper


def test_pause_preserves_wrapper_and_refreshes_its_exact_child_pin(policy):
    m,old,wrapper=policy;m.apply()
    updated=m.ORIGINAL.read_bytes();current=m.CANONICAL.read_bytes()
    assert hashlib.sha256(updated).hexdigest().encode() in current
    assert b'guard.run_python_original' in current
    assert b'"radar", "prune_enabled": true' in updated
    assert b'"wxcam", "prune_enabled": false' in updated
    assert m.change(updated,True)==updated
    assert next(m.BASE.glob('source-policy-*/0-before')).read_bytes()==old


def test_partial_policy_install_rolls_back_both_pin_and_child(policy,monkeypatch):
    m,old,wrapper=policy;failed=[]
    def atomic(path,raw,meta):
        if path==m.CANONICAL and not failed:
            failed.append(True);raise OSError('injected replace failure')
        path.write_bytes(raw)
    monkeypatch.setattr(m,'atomic',atomic)
    with pytest.raises(OSError):m.apply()
    assert m.ORIGINAL.read_bytes()==old and m.CANONICAL.read_bytes()==wrapper


def test_live_worker_prevents_policy_mutation(policy,monkeypatch):
    m,old,wrapper=policy
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**kw:'activating\ninactive\n')
    with pytest.raises(BlockingIOError):m.apply()
    assert m.ORIGINAL.read_bytes()==old and m.CANONICAL.read_bytes()==wrapper
