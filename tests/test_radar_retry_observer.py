import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

path = Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/retry_observer.py'
spec = importlib.util.spec_from_file_location('retry_observer', path)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(observer.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(observer, 'JOURNALS', tmp_path)
    monkeypatch.setattr(observer, 'FENCE', tmp_path / 'fence')
    identity = {'unit': 'aurora-direct-retry-example.service', 'invocation_id': 'a' * 32, 'main_pid': 42}
    argv = ['/usr/bin/python3', '/pinned/launcher.py', '--apply']
    props = {'LoadState': 'loaded', 'ActiveState': 'failed', 'SubState': 'failed', 'Result': 'exit-code',
             'ExecMainStatus': '1', 'ExecMainPID': '42', 'InvocationID': 'a' * 32, 'ControlGroup': '',
             'ExecStart': '{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /pinned/launcher.py --apply ; }'}
    monkeypatch.setattr(observer, '_query', lambda unit: props.copy())
    monkeypatch.setattr(observer.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='lock refusal\n'))
    return identity, argv, '20261027-gws-only-20261111t153440z-0', props


def test_collect_authenticates_exact_unit_and_detects_empty_commit(tmp_path, setup):
    identity, argv, operation, _ = setup
    result = observer.collect(identity, argv, operation)
    assert result['worker_pids'] == []
    assert not any(result['artifacts'].values())
    (tmp_path / ('transfer-' + operation + '.jsonl.commit.jsonl')).touch()
    assert observer.collect(identity, argv, operation)['artifacts']['commit'] is True


def test_changed_worker_or_command_is_rejected(setup, monkeypatch):
    identity, argv, operation, props = setup
    with pytest.raises(ValueError, match='command or identity'):
        observer.collect(identity, argv[:-1], operation)
    changed = props.copy()
    changed['InvocationID'] = 'b' * 32
    samples = iter([props, changed])
    monkeypatch.setattr(observer, '_query', lambda unit: next(samples))
    with pytest.raises(ValueError, match='changed during observation'):
        observer.collect(identity, argv, operation)


def test_unavailable_evidence_directory_never_means_absence(setup, tmp_path, monkeypatch):
    identity, argv, operation, _ = setup
    monkeypatch.setattr(observer, 'JOURNALS', tmp_path / 'missing')
    with pytest.raises(ValueError, match='directory unavailable'):
        observer.collect(identity, argv, operation)


def test_evidence_reads_original_journals_and_fails_when_missing(setup, tmp_path):
    identity, argv, operation, _ = setup
    stem = tmp_path / ('transfer-' + operation + '.jsonl')
    stem.write_bytes(b'{"phase":"intent"}\n')
    Path(str(stem) + '.preflight.json').write_bytes(b'{"expected":{}}\n')
    result = observer.evidence(identity, argv, operation, {'size': 1}, gws_absent=True,
                               stage_proof={}, source_proof={}, holds_ok=True, capacity_ok=True)
    assert result['journal_raw'] == stem.read_bytes()
    assert result['recorded_identity'] == identity
    stem.unlink()
    with pytest.raises(FileNotFoundError):
        observer.evidence(identity, argv, operation, {'size': 1}, gws_absent=True,
                          stage_proof={}, source_proof={}, holds_ok=True, capacity_ok=True)
