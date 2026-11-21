"""Prior receipts must not replace the live object-isolation proof."""
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
import gws_only_runner as runner
import gws_object_isolation


@pytest.fixture
def prior_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'BASE', tmp_path)
    receipt = tmp_path / 'direct-gws-only-preparation-test/captured-gws-evidence/verification.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({
        'state': 'gws-present-verified', 'object_verified': False,
        'eviction_authorized': False, 'retention_authorized': False,
        'expected': {'relative_path': 'Y2026/M11/D08/prior.LV1'},
    }))
    with sqlite3.connect(tmp_path / 'working-ledger.sqlite') as db:
        for table in ('provisional_object_versions', 'provisional_gws_versions'):
            db.execute(f'CREATE TABLE {table} (relative_path TEXT)')
        db.execute("INSERT INTO provisional_object_versions VALUES ('Y2026/M11/D09/other.LV1')")
    return tmp_path


def test_prior_capture_without_object_guard_needs_no_isolation_probe(prior_capture, monkeypatch):
    def unexpected_probe(*args, **kwargs):
        pytest.fail('an ordinary prior receipt must not trigger an isolation probe')
    monkeypatch.setattr(gws_object_isolation, 'check', unexpected_probe)
    assert runner.known_paths() == {'Y2026/M11/D08/prior.LV1', 'Y2026/M11/D09/other.LV1'}


@pytest.mark.parametrize('changed', [False, True])
def test_prior_capture_preserves_isolation_proof_for_recheck(prior_capture, monkeypatch, changed):
    (prior_capture / 'gws-pending-object-unresolved.json').write_text('{}')
    calls = []
    def check(candidate, operation, **kwargs):
        calls.append((candidate, operation, kwargs))
        return {'object_isolated': True, 'identity': 2 if changed and len(calls) > 1 else 1}
    monkeypatch.setattr(gws_object_isolation, 'check', check)
    kwargs = {'isolated_candidate': 'candidate', 'object_operation': 'operation', 'lock_fds': [17]}
    if changed:
        with pytest.raises(ValueError, match='object isolation changed'):
            runner.known_paths(**kwargs)
    else:
        assert 'Y2026/M11/D08/prior.LV1' in runner.known_paths(**kwargs)
    assert len(calls) == 2
    assert calls[0] == calls[1]
