import gzip
import io
import json
import os
from pathlib import Path

import pytest

from test_stage_native_version import fixture
from stage_native_version import stage
from send_finalized_stage import send


def test_sender_requires_fresh_handoff_and_exact_admission(tmp_path):
    root, destination, source, eligibility, captures, now = fixture(tmp_path)
    op, after = 'c'*32, 'd'*32
    proof = stage(root, destination, eligibility, op, captures=captures, minimum_free_bytes=0, now=now+1)
    evidence = {'schema': 'radar-protected-stage-handoff-v1', 'stage_operation': op, 'post_stage_operation': after,
                'source': proof['source'], 'size': proof['size'], 'sha256': proof['sha256'],
                'source_mtime_ns': proof['source_identity'][3], 'stage_identity': proof['stage_identity'],
                'checked_at': now, 'post_stage_observed_at': now, 'finalization_invocation_id': 'e'*32,
                'fast_archive_authorized': False, 'retention_authorized': False, 'eviction_authorized': False}
    path = destination / op / ('delivery-handoff-' + after + '.json')
    path.write_text(json.dumps(evidence)); path.chmod(0o440)
    expected = {'relative_path': evidence['source'], 'size': evidence['size'],
                'mtime_ns': evidence['source_mtime_ns'], 'sha256': evidence['sha256']}
    output = io.BytesIO()
    receipt = send(destination, op, after, expected, output, owner=os.getuid(), clock=lambda: now+1)
    assert gzip.decompress(output.getvalue()) == source.read_bytes()
    assert receipt['finalization_invocation_id'] == 'e'*32
    with pytest.raises(ValueError, match='admission'):
        send(destination, op, after, {**expected, 'mtime_ns': 1}, io.BytesIO(), owner=os.getuid(), clock=lambda: now+1)
    with pytest.raises(ValueError, match='stale'):
        send(destination, op, after, expected, io.BytesIO(), owner=os.getuid(), clock=lambda: now+301)
