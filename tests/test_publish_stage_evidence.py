from types import SimpleNamespace
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
import publish_stage_evidence as handoff


def test_handoff_requires_fresh_matching_proof_and_service(monkeypatch):
    op, first, second, after = ('a'*32, 'b'*32, 'c'*32, 'd'*32)
    proof = {'source': 'source.LV1', 'successor': 'next.LV1', 'size': 10, 'sha256': 'e'*64,
             'stage_path': '/stage/a/source.LV1', 'stage_identity': [1, 2, 10, 3, 4, 1],
             'source_identity': [1, 2, 10, 7, 8, 1]}
    final = {**proof, 'schema': 'radar-native-finalized-evidence-v1', 'stage_operation': op,
             'first_operation': first, 'second_operation': second, 'post_stage_operation': after,
             'samples': 3, 'checked_at': 1000, 'post_stage_observed_at': 1000, 'fast_archive_authorized': False,
             'retention_authorized': False, 'eviction_authorized': False}
    monkeypatch.setattr(handoff, 'inspect', lambda *a: (proof, {'protected': True}))
    monkeypatch.setattr(handoff, 'read_state', lambda *a: final)
    monkeypatch.setattr(handoff, 'validated_structure', lambda *a: {'samples': 3})
    argv = (f'/usr/bin/python3 /var/lib/aurora-radar-fast-finalization-v4/finalize_from_journal.py '
            f'--first {first} --second {second} --after {after} --operation {op}')
    props = {'Result': 'success', 'ExecMainStatus': '0', 'ActiveState': 'active', 'InvocationID': 'f'*32,
             'ExecMainPID': '42', 'ExecStart': '{ argv[]=' + argv + ' ; }'}
    monkeypatch.setattr(handoff.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        stdout='\n'.join(f'{key}={value}' for key, value in props.items())))
    assert handoff.build_evidence(op, after, now=1300)['source_mtime_ns'] == 7
    with pytest.raises(ValueError, match='fresh'):
        handoff.build_evidence(op, after, now=1301)
    props['Result'] = 'failed'
    with pytest.raises(ValueError, match='service'):
        handoff.build_evidence(op, after, now=1001)
