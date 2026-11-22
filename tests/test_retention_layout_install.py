"""A failed checker rollout restores every changed component before admissions resume."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import types

import pytest

ROOT = Path(__file__).parents[1] / 'roles/radar_raw_relocation/files'


@pytest.fixture
def release(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location('installer', ROOT / 'install-retention-layout-release.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    destination = tmp_path / 'installed'; destination.mkdir()
    package = tmp_path / 'package'; package.mkdir()
    operation = tmp_path / 'operation'; operation.mkdir()
    files = [destination / 'a.py', destination / 'b.py']
    rows = []
    for index, path in enumerate(files):
        old = ('VALUE = ' + str(index) + '\n').encode()
        new = ('VALUE = ' + str(index + 10) + '\n').encode()
        path.write_bytes(old)
        staged = package / str(path).lstrip('/'); staged.parent.mkdir(parents=True, exist_ok=True); staged.write_bytes(new)
        rows.append({'path': str(path), 'before_sha256': hashlib.sha256(old).hexdigest(),
                     'after_sha256': hashlib.sha256(new).hexdigest()})
    (package / 'release.json').write_text(json.dumps({'schema': 'radar-retention-layout-release-v1', 'changes': rows}))
    instance = types.SimpleNamespace(folder=operation, state={'safe_to_resume': True}, reentered=0)
    instance.save = lambda: None
    instance.unsafe = lambda: instance.state.update(safe_to_resume=False)
    instance.safe = lambda: instance.state.update(safe_to_resume=True)
    instance.handoff_check = lambda check: check()
    instance.reenter_exclusive = lambda: setattr(instance, 'reentered', instance.reentered + 1)
    class Window:
        def __init__(self, purpose): pass
        def __enter__(self): return instance
        def __exit__(self, *args): return False
    monkeypatch.setattr(module, 'Maintenance', Window)
    monkeypatch.setattr(module, 'REGISTRY', tmp_path / 'no-routes')
    monkeypatch.setattr(module, 'supported', lambda p: str(p).startswith(str(destination) + '/'))
    # The control machine is unprivileged; ownership changes are not the subject
    # of these fault-injection tests. Actual atomic writes/fsync/rename still run.
    monkeypatch.setattr(module.os, 'fchown', lambda *args: None)
    monkeypatch.setattr(module, 'validate', lambda: {'historical_continuity': {'files': 640}})
    return module, package, files, instance


def test_any_before_image_mismatch_prevents_all_changes(release):
    module, package, files, instance = release
    files[1].write_text('VALUE = 99\n')
    with pytest.raises(ValueError, match='changed'):
        module.install(package)
    assert files[0].read_text() == 'VALUE = 0\n'
    assert files[1].read_text() == 'VALUE = 99\n'
    assert instance.state['safe_to_resume']


def test_write_failure_rolls_back_previously_installed_component(release, monkeypatch):
    module, package, files, instance = release
    atomic = module.atomic
    def failing(path, raw, metadata):
        if path == files[1] and raw == b'VALUE = 11\n':
            raise OSError('simulated write failure')
        return atomic(path, raw, metadata)
    monkeypatch.setattr(module, 'atomic', failing)
    with pytest.raises(OSError, match='write failure'):
        module.install(package)
    assert [p.read_text() for p in files] == ['VALUE = 0\n', 'VALUE = 1\n']
    assert instance.state['rolled_back'] and instance.state['safe_to_resume']
    assert instance.reentered == 1


def test_failed_normal_admission_probe_rolls_back_entire_release(release, monkeypatch):
    module, package, files, instance = release
    calls = []
    def probe():
        calls.append([p.read_text() for p in files])
        if len(calls) == 1:
            raise ValueError('normal admission rejected')
        assert calls[-1] == ['VALUE = 0\n', 'VALUE = 1\n']
        return {'files': 640}
    monkeypatch.setattr(module, 'validate', probe)
    with pytest.raises(ValueError, match='admission rejected'):
        module.install(package)
    assert calls[0] == ['VALUE = 10\n', 'VALUE = 11\n']
    assert instance.state['safe_to_resume']


def test_unexpected_third_party_edit_is_not_overwritten_by_rollback(release, monkeypatch):
    module, package, files, instance = release
    def probe():
        files[0].write_text('VALUE = 777\n')
        raise ValueError('failure after a separate edit')
    monkeypatch.setattr(module, 'validate', probe)
    with pytest.raises(ValueError, match='another writer'):
        module.install(package)
    assert files[0].read_text() == 'VALUE = 777\n'
    assert not instance.state['safe_to_resume']
