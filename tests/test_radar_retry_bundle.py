from datetime import datetime, timezone
import hashlib
import json
import sys
from pathlib import Path

import pytest

root = Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'
sys.path.insert(0, str(root))
import retry_attempts
import retry_bundle
import retry_launcher


@pytest.fixture
def sample(tmp_path):
    template = (root / 'coordinator/resume_stage_launcher_103414.py').read_bytes()
    expected = {'relative_path': 'Y2026/M11/D03/file.LV1', 'size': 10, 'mtime_ns': 1, 'sha256': 'a' * 64}
    authority = {'retention_authorized': False, 'eviction_authorized': False}
    operation = {'nonce': 'b' * 32}
    preflight = {'schema': 'radar-direct-backlog-preflight-v1', 'expected': expected,
                 'operation': operation, **authority}
    journal = [{'phase': 'intent', 'operation': operation, **expected, **authority},
               {'phase': 'stage-complete', 'source_stable': True, **expected, **authority}]
    launcher_args = {'template': template, 'template_sha256': hashlib.sha256(template).hexdigest(),
        'original_stamp': '20261111T153440Z',
        'original_worker': {'unit': 'aurora-direct-20261027-gws-only-20261111t153440z-0.service',
                            'invocation_id': 'c' * 32, 'main_pid': 1234},
        'preflight_raw': json.dumps(preflight).encode(),
        'journal_raw': b'\n'.join(json.dumps(event).encode() for event in journal),
        'original_request': {'operation_id': 'original', 'relative_path': expected['relative_path'],
                             'expected_source': {'identity': [0, 0, 10, 1, 0, 0, 0, 0, 0, 0], 'sha256': expected['sha256']}}}
    built = retry_launcher.build(**launcher_args, operation_id='d' * 32, attempt=1)
    continuity = {'checked_at': datetime.fromtimestamp(1000, timezone.utc).isoformat(), 'accepted': True,
                  'files': 640, 'plan_sha256': retry_bundle.PLAN_SHA256,
                  'release_authorized': False, 'fresh_payload_hash': False}
    observation = {'relative_path': expected['relative_path'], 'size': 10, 'mtime_seconds': 0, 'observed_at': 1000}
    inputs = {'launcher': built['launcher'], 'request': built['request'],
              'continuity': json.dumps(continuity).encode(), 'observation': json.dumps(observation).encode()}
    proof = {'state': 'precommit-lock-refusal', 'invocation_id': 'e' * 32,
             'journal_sha256': hashlib.sha256(launcher_args['journal_raw']).hexdigest(), 'expected': expected}
    intent = retry_attempts.reserve(tmp_path, 'd' * 32, 1, proof, inputs, 'gzip-v3-vm03')
    return intent, inputs, launcher_args


def test_bundle_binds_recorded_inputs_and_pinned_worker(sample):
    intent, inputs, launcher_args = sample
    bundle = retry_bundle.build(intent, inputs, launcher_args, resume_sha256='f' * 64,
                                worker_sha256='e' * 64, now=1100)
    assert bundle['unit'] == intent['unit']
    assert bundle['argv'][-1] == '--apply'
    assert bundle['files']['launcher.py'] == inputs['launcher']
    assert bundle['retention_authorized'] is False
    assert hashlib.sha256(inputs['request']).hexdigest().encode() in bundle['files']['SHA256SUMS']


def test_bundle_refuses_stale_or_replaced_inputs(sample):
    intent, inputs, launcher_args = sample
    with pytest.raises(ValueError, match='stale'):
        retry_bundle.build(intent, inputs, launcher_args, resume_sha256='f' * 64,
                           worker_sha256='e' * 64, now=1601)
    with pytest.raises(ValueError, match='differ from durable intent'):
        retry_bundle.build(intent, {**inputs, 'request': b'changed'}, launcher_args,
                           resume_sha256='f' * 64, worker_sha256='e' * 64, now=1100)
