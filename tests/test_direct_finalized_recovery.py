import contextlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


def load(monkeypatch):
    interrupt = ModuleType('direct_interrupt'); interrupt.protect = contextlib.nullcontext
    transport = ModuleType('direct_stage_pipeline')
    def transfer(*args, **kwargs):
        kwargs['safety_check']()
        return {'stage_receipt': {'stage': 'ok'}}
    transport.transfer = transfer
    commit = ModuleType('direct_staged_commit')
    commit.publish = lambda *a, **k: {'verified_stage': {'stage': 'ok'}}
    recovery = ModuleType('direct_staged_recovery_refresh')
    recovery._save_exclusive = lambda p, v: Path(p).write_text(json.dumps(v), encoding='utf-8')
    pending = ModuleType('finalized_expected'); pending.valid_expected = lambda value: isinstance(value, dict)
    for name, module in [('direct_interrupt', interrupt), ('direct_stage_pipeline', transport),
                         ('direct_staged_commit', commit), ('direct_staged_recovery_refresh', recovery),
                         ('finalized_expected', pending)]:
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/direct_finalized_recovery.py'
    spec = importlib.util.spec_from_file_location('isolated_finalized_recovery', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module, commit


class Adapter:
    def __init__(self, expected): self.expected = expected; self.calls = 0; self.changed = False
    def exclusive_recovery_locks(self): return contextlib.nullcontext()
    def set_deadline(self, deadline): pass
    def require_controls_capacity_and_hold(self): pass
    def authenticated_handoff(self, expected):
        self.calls += 1
        return {'identity': 'changed' if self.changed and self.calls > 2 else 'stable'}
    def require_local_absence(self, path): pass
    def require_gws_absence_and_capacity(self, path, size): pass
    def finalized_stage_commands(self, expected, handoff, nonce): return (['sender'], ['receiver'], b'sender', b'receiver')
    def refresh_continuity(self, path, timeout): pass
    def reconcile_successful_publication(self, proof, path, timeout): return {'outcome': {'state': 'present'}}
    def independent_gws_readback(self, expected, timeout): return {**self.expected, 'stable': True}


def test_reconciles_and_verifies_before_recording_success(tmp_path, monkeypatch):
    module, _ = load(monkeypatch)
    expected = {'relative_path': 'a', 'size': 5, 'mtime_ns': 1, 'sha256': 'b'}
    checks = []
    result = module.transfer_one(Adapter(expected), expected, tmp_path/'transfer', safety_check=lambda: checks.append(True))
    assert checks == [True]
    assert result['gws_verified'] is True and result['object_verified'] is False
    assert (tmp_path/'transfer.readback.json').is_file()


def test_changed_handoff_stops_before_publication(tmp_path, monkeypatch):
    module, commit = load(monkeypatch)
    expected = {'relative_path': 'a', 'size': 5, 'mtime_ns': 1, 'sha256': 'b'}
    adapter = Adapter(expected); adapter.changed = True
    monkeypatch.setattr(commit, 'publish', lambda *a, **k: pytest.fail('must not publish'))
    with pytest.raises(ValueError, match='changed before publication'):
        module.transfer_one(adapter, expected, tmp_path/'transfer', safety_check=lambda: None)
    assert (tmp_path/'transfer.preflight.json').is_file()
