"""A rejected live replacement must restore the old view without deleting originals."""
import importlib.util
import json
from pathlib import Path
import types

import pytest

ROOT = Path(__file__).parents[1] / 'roles/radar_raw_relocation/files'


@pytest.fixture
def cutover(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location('retirement', ROOT / 'aurora-cloud-radar-retention.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.ORIGINALS = tmp_path / 'originals'; module.STATE = tmp_path / 'receipts'; module.STATE.mkdir()
    module.REGISTRY = tmp_path / 'registry.json'
    day = 'Y2026/M05/D02'
    directory = module.ORIGINALS / day; directory.mkdir(parents=True)
    files = {}
    for i in range(96):
        path = directory / ('observation-' + str(i)); path.write_bytes(('science ' + str(i)).encode())
        files[path.name] = {'signature': module.signature(path.stat())}
    route = {'image_sha256': 'a' * 64}
    unit = tmp_path / 'day.mount'; unit.write_bytes(b'OLD MOUNT\n')
    mounted = {'new': False, 'force_image_rejection': False}
    operation = tmp_path / 'operation'; operation.mkdir()
    state = {}
    window = types.SimpleNamespace(folder=operation, state=state)
    window.save = lambda: None
    window.unsafe = lambda: state.update(safe=False)
    window.safe = lambda: state.update(safe=True)
    window.reenter_exclusive = lambda: None
    window.handoff_check = lambda check: check()
    class Window:
        def __init__(self, purpose): pass
        def __enter__(self): return window
        def __exit__(self, *args): return False
    def command(args, **kwargs):
        if args[:2] == ['systemctl', 'stop']: mounted['new'] = False
        if args[:2] == ['systemctl', 'start']: mounted['new'] = unit.read_bytes() == b'NEW MOUNT\n'
        return ''
    utility = types.SimpleNamespace(control=lambda p: p.read_bytes(), run=command, syncdir=lambda p: None)
    def routes():
        return (json.loads(module.REGISTRY.read_text())['routes'], 'pin') if module.REGISTRY.exists() else ({}, None)
    def guard(day, record):
        if not mounted['new'] or mounted['force_image_rejection']: raise ValueError('image view rejected')
    helper = types.SimpleNamespace(routes=routes, guard=guard)
    class Loader:
        def __init__(self, *args): pass
        def load_module(self):
            return types.SimpleNamespace(atomic=lambda p, raw, meta: p.write_bytes(raw))
    monkeypatch.setattr(module, 'SourceFileLoader', Loader)
    monkeypatch.setattr(module, 'Maintenance', Window)
    monkeypatch.setattr(module, 'helper', lambda: helper)
    monkeypatch.setattr(module, 'tools', lambda: utility)
    monkeypatch.setattr(module, 'raw_gate', lambda: None)
    monkeypatch.setattr(module, 'archive_evidence', lambda *a: {'files': files})
    monkeypatch.setattr(module, 'image_route', lambda d: route)
    monkeypatch.setattr(module, 'mount_unit', lambda d: ('day.mount', unit, b'NEW MOUNT\n'))
    def forbidden(*a, **kw): raise AssertionError('duplicate deletion reached before live acceptance')
    monkeypatch.setattr(module, 'remove_duplicates', forbidden)
    return module, day, directory, unit, mounted, window


def test_image_rejection_restores_original_mount_definition(cutover, monkeypatch):
    module, day, originals, unit, mounted, window = cutover
    mounted['force_image_rejection'] = True
    monkeypatch.setattr(module, 'validator', lambda: lambda: {'historical_files': 640})
    with pytest.raises(ValueError, match='image view rejected'):
        module.retire(day)
    assert len(list(originals.iterdir())) == 96
    assert unit.read_bytes() == b'OLD MOUNT\n'
    assert not module.REGISTRY.exists()
    assert window.state['safe']


def test_whole_cohort_rejection_rolls_back_registry_before_any_unlink(cutover, monkeypatch):
    module, day, originals, unit, mounted, window = cutover
    probes = []
    def validate():
        probes.append(mounted['new'])
        if mounted['new']: raise ValueError('historical cohort incomplete')
        return {'historical_files': 640}
    monkeypatch.setattr(module, 'validator', lambda: validate)
    with pytest.raises(ValueError, match='cohort incomplete'):
        module.retire(day)
    assert probes == [True, False]
    assert len(list(originals.iterdir())) == 96
    assert unit.read_bytes() == b'OLD MOUNT\n'
    assert not module.REGISTRY.exists()
    assert window.state['safe']
