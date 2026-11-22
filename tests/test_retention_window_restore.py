"""Interrupted windows restore protection before allowing archive admissions to resume."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).parents[1] / 'roles/ass_retention/files'


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location('window', ROOT / 'aurora-ass-retention-window.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.HOLD = tmp_path / 'hold'
    module.DROP = tmp_path / 'override'
    folder = tmp_path / 'window-test'; folder.mkdir()
    raw = b'original protection\n'
    module.HOLD_SHA = hashlib.sha256(raw).hexdigest()
    (folder / 'hold.original').write_bytes(raw)
    state = {'hold_mode': 0o644, 'hold_uid': 0, 'hold_gid': 0,
             'resume_cloud_units': ['mirror.timer'], 'resume_field_units': ['discover.timer'],
             'override': '[Service]\nExecStart=/signed --max-candidates 5\n'}
    (folder / 'state.json').write_text(json.dumps(state))
    module.DROP.write_text(state['override'])
    # Test recovery ordering on a non-root control machine; production control()
    # independently enforces root ownership and non-writable controls.
    monkeypatch.setattr(module, 'control', lambda p: p.read_bytes())
    monkeypatch.setattr(module.os, 'fchown', lambda *args: None)
    calls = []
    def run(args, **kwargs):
        assert module.HOLD.read_bytes() == raw
        calls.append(args)
        return ''
    def wait(unit, timeout):
        assert module.HOLD.read_bytes() == raw
        calls.append(['wait-for-signed-worker', unit])
    monkeypatch.setattr(module, 'run', run)
    monkeypatch.setattr(module, 'wait_inactive', wait)
    monkeypatch.setattr(module, 'props', lambda unit: {'ActiveState': 'inactive', 'MainPID': '0', 'ControlPID': '0'})
    return module, folder, raw, calls


def test_restore_hold_then_drain_signed_worker_then_resume(window):
    module, folder, raw, calls = window
    module.restore(folder)
    assert module.HOLD.read_bytes() == raw
    assert not module.DROP.exists()
    wait = next(i for i, call in enumerate(calls) if call[0] == 'wait-for-signed-worker')
    resume = next(i for i, call in enumerate(calls) if call[:2] == ['systemctl', 'start'])
    assert wait < resume
    assert json.loads((folder / 'state.json').read_text())['restored_at']


def test_partial_restore_keeps_admissions_paused(window):
    module, folder, raw, calls = window
    module.restore(folder, resume=False)
    assert module.HOLD.read_bytes() == raw
    assert all(call[:2] != ['systemctl', 'start'] for call in calls)
    assert 'restored_at' not in json.loads((folder / 'state.json').read_text())


def test_conflicting_hold_is_never_overwritten(window):
    module, folder, _, calls = window
    module.HOLD.write_bytes(b'a newer operator decision')
    with pytest.raises(ValueError, match='refuse automatic overwrite'):
        module.restore(folder)
    assert module.HOLD.read_bytes() == b'a newer operator decision'
    assert calls == []


def test_unknown_worker_override_is_not_removed(window):
    module, folder, raw, calls = window
    module.DROP.write_text('a newer worker override')
    with pytest.raises(ValueError, match='override changed'):
        module.restore(folder)
    assert module.HOLD.read_bytes() == raw
    assert module.DROP.read_text() == 'a newer worker override'
    assert calls == []


def test_already_restored_window_does_not_repeat_starts(window):
    module, folder, _, calls = window
    module.restore(folder)
    calls.clear()
    module.restore(folder)
    assert calls == []


def test_boot_restore_does_not_deadlock_on_signed_start_queued_behind_it(window, monkeypatch):
    module, folder, raw, calls = window
    monkeypatch.setattr(module, 'props', lambda unit: {'ActiveState': 'activating', 'MainPID': '0', 'ControlPID': '0'})
    module.restore(folder)
    assert module.HOLD.read_bytes() == raw
    assert not any(call[0] == 'wait-for-signed-worker' for call in calls)


def test_expired_archive_gate_defers_without_changing_admissions(window, monkeypatch):
    module, folder, raw, calls=window
    mirror=folder/'mirror';mirror.mkdir();(mirror/'summary.json').write_text('{}')
    gate=folder/'gate.json';gate.write_text('{}')
    def unready(*args):raise ValueError('raw retention archive gate is not ready')
    monkeypatch.setattr(module,'require_ready',unready)
    with pytest.raises(module.GateDeferred,match='not ready'):
        module.mirror_gate({'MIRROR':mirror,'OBJECT_GATE':gate,'STREAMS':[]})
    assert calls==[]
    assert module.DROP.exists()  # No admission, override or hold mutation.
