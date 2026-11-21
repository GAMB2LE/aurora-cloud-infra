import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
from finalized_gws_adapter import FinalizedStageMixin, BOOTSTRAP


class Parent:
    def staged_direct_commands(self, expected, identity, nonce):
        return ['ssh', 'old'], ['ssh', 'receiver'], b'old', b'pinned-receiver'


class Adapter(FinalizedStageMixin, Parent):
    def _field(self, code, pins, action, op, after):
        assert code == BOOTSTRAP and action == 'inspect'
        return {'source': 'Y2026/M11/D13/example.LV1', 'size': 10, 'source_mtime_ns': 12,
                'sha256': 'e'*64, 'stage_operation': op, 'post_stage_operation': after}


def test_exact_handoff_and_sender_binding():
    adapter = Adapter(); adapter.configure_finalized_stage('a'*32, 'b'*32)
    expected = {'relative_path': 'Y2026/M11/D13/example.LV1', 'size': 10, 'mtime_ns': 12, 'sha256': 'e'*64}
    handoff = adapter.authenticated_handoff(expected)
    sender, receiver, source, destination = adapter.finalized_stage_commands(expected, handoff, 'c'*32)
    assert sender[0] == 'ssh' and 'send' in sender[-1] and receiver == ['ssh', 'receiver']
    assert source.startswith(BOOTSTRAP.encode()) and destination == b'pinned-receiver'
    with pytest.raises(ValueError, match='handoff'):
        adapter.authenticated_handoff({**expected, 'sha256': 'f'*64})
    with pytest.raises(ValueError, match='operation'):
        adapter.finalized_stage_commands(expected, handoff, 'invalid')
