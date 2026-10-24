"""Disposable fixtures for the bounded historical quarantine preservation."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts/preserve_old_radar_quarantine_20261024.py'
spec = importlib.util.spec_from_file_location('old_radar', MODULE_PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'OWNER', os.geteuid())
    if not hasattr(os, 'listxattr'):
        # macOS stdlib lacks this Linux API. Attribute preservation has a
        # separate real Linux test; these fixtures contain no attributes.
        monkeypatch.setattr(os, 'listxattr', lambda *a, **k: [], raising=False)
    source = tmp_path / 'old-mirror' / 'rpgfmcw94'
    source.mkdir(parents=True)
    (source / 'Y2026' / 'D01').mkdir(parents=True)
    (source / 'Y2026' / 'D02').mkdir()
    for name, data in [('Y2026/D01/a.NC', b'first science' * 100), ('Y2026/D01/b.LV0', b'raw' * 100),
                       ('Y2026/D02/c.LV1', b'third' * 300)]:
        (source / name).write_bytes(data)
    for base, dirs, files in os.walk(source):
        os.chmod(base, 0o2770)
        for name in files:
            os.chmod(Path(base) / name, 0o640)
    baseline = m.inventory(source)
    target = tmp_path / 'root-target' / 'rpgfmcw94'
    request = {'source': str(source), 'target': str(target), 'backup_parent': str(tmp_path / 'private-backup'),
               'root_device': source.stat().st_dev, 'data_device': source.stat().st_dev,
               'root_reserve_bytes': 100 * m.GIB}
    receipt = tmp_path / 'receipt.json'
    monkeypatch.setattr(m.Preserver, 'capacities', lambda self, remaining: 200 * m.GIB)
    monkeypatch.setattr(m.Preserver, 'check_closure', lambda self: None)
    monkeypatch.setattr(m.Preserver, 'check_references', lambda self: None)
    monkeypatch.setattr(m, 'protected_parent', lambda path: None)
    def make():
        return m.Preserver(request, baseline, 'request-sha', 'tool-sha', receipt)
    return make, source, target, baseline


def payloads(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def crash(runner, event):
    def hook(name):
        if name == event:
            raise InterruptedError(event)
    runner.event = hook


def test_complete_and_readonly_rerun(fixture):
    make, source, target, baseline = fixture
    before = payloads(source)
    result = make().run()
    assert result['phase'] == 'complete'
    assert source.is_symlink() and source.resolve() == target
    assert payloads(source) == before == payloads(target)
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o755
    for name, entry in m.inventory(target).items():
        assert m.attributes(entry) == m.attributes(baseline[name])
    before_receipt = make().receipt.read_bytes()
    assert make().run(resume=True) == result
    assert make().receipt.read_bytes() == before_receipt


@pytest.mark.parametrize('event', ['mkdir', 'temporary-created', 'copy-chunk', 'copy-promoted',
    'target-hashed', 'target-renamed', 'source-renamed', 'symlink-created',
    'release-intent', 'release-unlinked', 'rmdir-intent', 'rmdir-done', 'access-restored'])
def test_each_interruption_resumes_without_losing_payload(fixture, event):
    make, source, target, _ = fixture
    before = payloads(source)
    runner = make(); crash(runner, event)
    with pytest.raises(InterruptedError, match=event):
        runner.run()
    result = make().run(resume=True)
    assert result['phase'] == 'complete'
    assert payloads(source) == before == payloads(target)


@pytest.mark.parametrize('kind', ['source-bytes', 'source-mode', 'source-extra', 'source-missing',
                                 'target-bytes', 'target-mode', 'target-extra', 'target-missing'])
def test_changed_tree_refuses_and_preserves_originals(fixture, kind):
    make, source, _, baseline = fixture
    runner = make(); crash(runner, 'target-hashed')
    with pytest.raises(InterruptedError): runner.run()
    tree = source if kind.startswith('source') else runner.stage
    file = tree / runner.files[0]
    if kind.endswith('bytes'): file.write_bytes(b'changed')
    if kind.endswith('mode'): file.chmod(0o600)
    if kind.endswith('extra'): (tree / 'foreign').write_text('foreign')
    if kind.endswith('missing'): file.unlink()
    before = payloads(source)
    with pytest.raises(RuntimeError): make().run(resume=True)
    assert payloads(source) == before
    assert not source.is_symlink()


@pytest.mark.parametrize('kind', ['wrong-symlink', 'target-inode', 'target-bytes', 'target-extra', 'backup-missing'])
def test_publication_proof_rejects_changes_before_release(fixture, tmp_path, kind):
    make, source, target, _ = fixture
    runner = make(); crash(runner, 'release-intent')
    with pytest.raises(InterruptedError): runner.run()
    before = payloads(runner.backup)
    if kind == 'wrong-symlink':
        source.unlink(); source.symlink_to(tmp_path)
    elif kind == 'target-inode':
        path = target / runner.files[0]; data = path.read_bytes(); path.unlink(); path.write_bytes(data)
    elif kind == 'target-bytes': (target / runner.files[0]).write_bytes(b'bad')
    elif kind == 'target-extra': (target / 'foreign').touch()
    elif kind == 'backup-missing': (runner.backup / runner.files[-1]).unlink()
    after_mutation = payloads(runner.backup)
    with pytest.raises(RuntimeError): make().run(resume=True)
    assert payloads(runner.backup) == after_mutation
    if kind != 'backup-missing': assert after_mutation == before


def test_target_ancestor_stays_private_until_all_unlinks(fixture):
    make, _, target, _ = fixture
    runner = make()
    def hook(event):
        if event != 'access-restored' and target.parent.exists():
            assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    runner.event = hook
    runner.run()
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o755


def test_opened_target_ancestor_refuses_release(fixture):
    make, _, target, _ = fixture
    runner = make(); crash(runner, 'release-intent')
    with pytest.raises(InterruptedError): runner.run()
    before = payloads(runner.backup)
    target.parent.chmod(0o755)
    with pytest.raises(RuntimeError, match='Private'): make().run(resume=True)
    assert payloads(runner.backup) == before


@pytest.mark.parametrize('kind', ['request', 'tool', 'baseline', 'malformed', 'missing'])
def test_foreign_receipt_cannot_resume(fixture, kind):
    make, source, _, _ = fixture
    runner = make(); crash(runner, 'temporary-created')
    with pytest.raises(InterruptedError): runner.run()
    if kind == 'request': runner = make(); runner.pins['request_sha256'] = 'foreign'
    if kind == 'tool': runner = make(); runner.pins['tool_sha256'] = 'foreign'
    if kind == 'baseline': runner = make(); runner.baseline = {}
    if kind == 'malformed': runner.receipt.write_text('not json')
    if kind == 'missing': runner.receipt.unlink()
    before = payloads(source)
    with pytest.raises((RuntimeError, ValueError, FileNotFoundError)): runner.run(resume=True)
    assert payloads(source) == before


def test_staging_only_receipt_cannot_release(fixture):
    make, source, _, _ = fixture
    runner = make(); crash(runner, 'target-hashed')
    with pytest.raises(InterruptedError): runner.run()
    before = payloads(source)
    runner.load()
    with pytest.raises(RuntimeError, match='Staging-only'): runner.release()
    assert payloads(source) == before


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'directory-symlink'])
def test_links_rejected(fixture, kind):
    make, source, _, _ = fixture
    file = source / 'Y2026/D01/a.NC'
    if kind == 'symlink': (source / 'link').symlink_to(file)
    elif kind == 'hardlink': os.link(file, source / 'hardlink')
    else: (source / 'linkdir').symlink_to(source / 'Y2026', target_is_directory=True)
    with pytest.raises(RuntimeError): make().preflight()


def test_foreign_container_rejected(fixture):
    make, source, target, _ = fixture
    target.parent.mkdir()
    before = payloads(source)
    with pytest.raises(RuntimeError, match='Pre-existing'): make().run()
    assert payloads(source) == before


def test_receipt_replacement_during_run_stops(fixture):
    make, source, _, _ = fixture
    runner = make()
    def hook(event):
        if event == 'temporary-created':
            runner.receipt.unlink(); runner.receipt.write_text('{}')
    runner.event = hook
    before = payloads(source)
    with pytest.raises(RuntimeError, match='Receipt'): runner.run()
    assert payloads(source) == before


def test_optimized_python_refuses_even_help():
    result = subprocess.run([sys.executable, '-O', str(MODULE_PATH), '--help'], capture_output=True, text=True)
    assert result.returncode != 0 and 'without Python optimization' in result.stderr


@pytest.mark.parametrize('root,alias_root,alias_path,device', [
    ('/data/old/radar', '/old/radar', '/elsewhere', 2),
    ('/data/old/radar', '/old', '/elsewhere', 2),
    ('/data/old/radar', '/old/radar/file', '/elsewhere', 2),
    ('/var/lib/recovery/radar', '/var/lib/recovery/radar', '/elsewhere', 1),
    ('/var/lib/recovery/radar', '/var/lib', '/elsewhere', 1),
    ('/data/old/radar', '/', '/data/old/radar/nested', 3),
])
def test_external_and_nested_mount_aliases_refuse(root, alias_root, alias_path, device):
    host = [{'device': 1, 'root': Path('/'), 'path': Path('/')},
            {'device': 2, 'root': Path('/'), 'path': Path('/data')}]
    record = {'device': device, 'root': Path(alias_root), 'path': Path(alias_path)}
    with pytest.raises(RuntimeError, match='mount'):
        m.reject_mount_aliases([Path(root)], host, host + [record])


def test_canonical_mounts_are_allowed():
    host = [{'device': 1, 'root': Path('/'), 'path': Path('/')},
            {'device': 2, 'root': Path('/'), 'path': Path('/data')}]
    m.reject_mount_aliases([Path('/data/old/radar'), Path('/var/lib/recovery/radar')], host, host)


@pytest.mark.parametrize('method', ['check_closure', 'check_references', 'capacities'])
def test_failed_admission_never_starts_copy(fixture, monkeypatch, method):
    make, source, target, _ = fixture
    before = payloads(source)
    def reject(*args): raise RuntimeError('failed admission')
    monkeypatch.setattr(m.Preserver, method, reject)
    with pytest.raises(RuntimeError, match='admission'): make().run()
    assert payloads(source) == before and not target.parent.exists()


def test_changed_source_during_full_target_verification_stops(fixture):
    make, source, _, _ = fixture
    runner = make()
    def hook(event):
        if event == 'target-hashed': (source / runner.files[-1]).write_bytes(b'changed during independent read')
    runner.event = hook
    with pytest.raises(RuntimeError, match='Original'): runner.run()
    assert not source.is_symlink() and not runner.backup.exists()


def test_earlier_target_change_during_later_hash_stops(fixture):
    make, source, _, _ = fixture
    runner = make(); count = 0
    def hook(event):
        nonlocal count
        if event == 'target-hashed':
            count += 1
            if count == len(runner.files): (runner.stage / runner.files[0]).write_bytes(b'late mutation')
    runner.event = hook
    before = payloads(source)
    with pytest.raises(RuntimeError, match='copy changed'): runner.run()
    assert payloads(source) == before and not source.is_symlink()


def test_target_directory_swap_stops_before_unlink(fixture):
    make, _, target, _ = fixture
    runner = make(); crash(runner, 'release-intent')
    with pytest.raises(InterruptedError): runner.run()
    before = payloads(runner.backup)
    os.rename(target, target.with_name('swapped'))
    target.mkdir()
    with pytest.raises(RuntimeError): make().run(resume=True)
    assert payloads(runner.backup) == before


def test_pending_does_not_authorize_other_missing_original(fixture):
    make, _, _, _ = fixture
    runner = make(); crash(runner, 'release-intent')
    with pytest.raises(InterruptedError): runner.run()
    (runner.backup / runner.files[-1]).unlink()
    before = payloads(runner.backup)
    with pytest.raises(RuntimeError, match='membership'): make().run(resume=True)
    assert payloads(runner.backup) == before


@pytest.mark.skipif(sys.platform != 'linux' or os.geteuid() != 0, reason='real root Linux ownership/xattr fixture')
def test_linux_root_ownership_setgid_and_xattrs(fixture):
    make, source, target, _ = fixture
    for path in [source, *source.rglob('*')]:
        os.chown(path, 56781, 56781)
        os.chmod(path, 0o2770 if path.is_dir() else 0o660)
        os.setxattr(path, 'user.preservation-fixture', b'unchanged')
    baseline = m.inventory(source)
    runner = make(); runner.baseline = baseline
    runner.run()
    for name, entry in m.inventory(target).items():
        assert m.attributes(entry) == m.attributes(baseline[name])
        assert entry['identity'][5:7] == [56781, 56781]


def test_namespace_retries_a_surviving_representative(monkeypatch):
    expected = [{'device': 1, 'root': Path('/'), 'path': Path('/')}]
    def records(path):
        if path == Path('/proc/first/mountinfo'): raise FileNotFoundError()
        return expected
    monkeypatch.setattr(m, 'mount_records', records)
    assert m.namespace_records([Path('/proc/first/mountinfo'), Path('/proc/second/mountinfo')]) == expected


def test_namespace_disappearance_fails_closed(monkeypatch):
    def records(path): raise FileNotFoundError()
    monkeypatch.setattr(m, 'mount_records', records)
    with pytest.raises(RuntimeError, match='namespace disappeared'):
        m.namespace_records([Path('/proc/first/mountinfo')])


def test_logical_link_replaced_after_release_intent_does_not_unlink(fixture, tmp_path):
    make, source, _, _ = fixture
    runner = make()
    def hook(event):
        if event == 'release-intent':
            source.unlink(); source.symlink_to(tmp_path)
    runner.event = hook
    before = payloads(source)
    with pytest.raises(RuntimeError, match='Logical link changed'): runner.run()
    assert payloads(runner.backup) == before


def test_inventory_propagates_walk_errors(fixture, monkeypatch):
    _, source, _, _ = fixture
    def failing_walk(root, **kwargs):
        kwargs['onerror'](PermissionError('unreadable subtree'))
        yield
    monkeypatch.setattr(os, 'walk', failing_walk)
    with pytest.raises(PermissionError, match='unreadable'): m.inventory(source)


def test_publication_closure_failure_keeps_full_byte_proof(fixture, monkeypatch):
    make, source, _, _ = fixture
    runner = make(); calls = 0
    def closure(self):
        nonlocal calls
        calls += 1
        if calls == 2: raise RuntimeError('namespace disappeared')
    monkeypatch.setattr(m.Preserver, 'check_closure', closure)
    with pytest.raises(RuntimeError, match='namespace disappeared'): runner.run()
    receipt = json.loads(runner.receipt.read_bytes())
    assert receipt['phase'] == 'publishing' and receipt['copy_proof']
    assert source.is_dir() and not source.is_symlink()
    def unexpected_hash(*args): raise RuntimeError('unnecessary full rehash')
    monkeypatch.setattr(m, 'hash_file', unexpected_hash)
    assert make().run(resume=True)['phase'] == 'complete'


def test_completed_rerun_checks_traversal(fixture):
    make, _, target, _ = fixture
    make().run()
    target.parent.chmod(0o700)
    with pytest.raises(RuntimeError, match='Completed target traversal'): make().run(resume=True)
