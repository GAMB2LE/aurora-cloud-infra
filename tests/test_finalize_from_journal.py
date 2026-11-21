import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

FILES = Path(__file__).resolve().parents[1] / 'roles/radar_archive_recovery/files'
sys.path.insert(0, str(FILES))
from finalize_from_journal import validated_structure


def test_validation_service_identity_and_limits_fail_closed(monkeypatch):
    operation = 'a' * 32
    invocation = 'b' * 32
    proof = {'worker_unit': 'aurora-radar-lv1-validate-' + operation + '.service',
             'worker_invocation_id': invocation, 'worker_pid': 42}
    monkeypatch.setattr('finalize_from_journal.read_state', lambda _: proof)
    props = {'InvocationID': invocation, 'ExecMainPID': '42', 'Result': 'success',
             'ExecMainStatus': '0', 'ActiveState': 'active', 'MemoryMax': '805306368',
             'RuntimeMaxUSec': '10min'}
    monkeypatch.setattr('finalize_from_journal.subprocess.run',
                        lambda *a, **k: SimpleNamespace(stdout='\n'.join(f'{k}={v}' for k, v in props.items())))
    assert validated_structure(operation) == proof
    for key, bad in [('InvocationID', 'c' * 32), ('ExecMainPID', '43'), ('Result', 'failed'),
                     ('ExecMainStatus', '1'), ('ActiveState', 'inactive'), ('MemoryMax', 'infinity'),
                     ('RuntimeMaxUSec', 'infinity')]:
        original = props[key]
        props[key] = bad
        with pytest.raises(ValueError):
            validated_structure(operation)
        props[key] = original
