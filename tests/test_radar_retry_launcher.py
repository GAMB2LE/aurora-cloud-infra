import hashlib
import ast
import importlib.util
import json
from pathlib import Path

import pytest

root = Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'
spec = importlib.util.spec_from_file_location('retry_launcher', root / 'retry_launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.fixture
def args():
    template = (root / 'coordinator/resume_stage_launcher_103414.py').read_bytes()
    expected = {'relative_path': 'Y2026/M11/D03/file.LV1', 'size': 10, 'mtime_ns': 1, 'sha256': 'a' * 64}
    authority = {'retention_authorized': False, 'eviction_authorized': False}
    operation = {'nonce': 'b' * 32}
    preflight = {'schema': 'radar-direct-backlog-preflight-v1', 'expected': expected,
                 'operation': operation, **authority}
    events = [{'phase': 'intent', 'operation': operation, **expected, **authority},
              {'phase': 'stage-complete', 'source_stable': True, **expected, **authority}]
    return dict(template=template, template_sha256=hashlib.sha256(template).hexdigest(),
                original_stamp='20261111T153440Z',
                original_worker={'unit': 'aurora-direct-20261027-gws-only-20261111t153440z-0.service',
                                 'invocation_id': 'c' * 32, 'main_pid': 1234},
                preflight_raw=json.dumps(preflight).encode(),
                journal_raw=b'\n'.join(json.dumps(event).encode() for event in events),
                original_request={'operation_id': 'original', 'relative_path': expected['relative_path'],
                                  'expected_source': {'identity': [0, 0, 10, 1, 0, 0, 0, 0, 0, 0],
                                                      'sha256': expected['sha256']}},
                operation_id='d' * 32, attempt=1)


def test_launcher_preserves_original_stage_and_binds_new_unit(args):
    result = launcher.build(**args)
    assert result['unit'] == 'aurora-direct-retry-' + 'd'*32 + '-000001.service'
    assert json.loads(result['request'])['operation_id'] == 'retry-' + 'd'*32 + '-000001'
    text = result['launcher'].decode()
    assert 'transfer-20261027-gws-only-20261111t153440z-0.jsonl' in text
    assert result['unit'] in text
    assert args['original_worker']['invocation_id'] in text
    assert hashlib.sha256(args['journal_raw']).hexdigest() in text


def test_launcher_rejects_changed_pins_and_original_worker(args):
    with pytest.raises(ValueError, match='template'):
        launcher.build(**{**args, 'template_sha256': '0' * 64})
    with pytest.raises(ValueError, match='worker identity'):
        launcher.build(**{**args, 'original_worker': {**args['original_worker'], 'unit': 'different.service'}})


def test_vm01_launcher_requires_original_release_pin(args):
    template = (root / 'coordinator/resume_stage_launcher_vm01.py').read_bytes()
    params = {**args, 'template': template, 'template_sha256': hashlib.sha256(template).hexdigest()}
    with pytest.raises(ValueError, match='launcher pin missing'):
        launcher.build(**params)
    result = launcher.build(**params, release_launcher_sha256='f' * 64)
    assert b'VM01_LAUNCHER_SHA256_PLACEHOLDER' not in result['launcher']
    assert b'f' * 64 in result['launcher']


def test_admission_launcher_is_readonly_and_exposes_existing_checks(args):
    original = launcher.build(**args)['launcher']
    generated = launcher.build_readonly_admission(original)
    tree = ast.parse(generated.decode())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == 'resume']
    assert len(calls) == 1
    assert next(k.value.value for k in calls[0].keywords if k.arg == 'apply') is False
    assert b"admission launcher is read-only" in generated
    assert b"_retry_source_proof" in generated and b"_retry_stage_proof" in generated
    with pytest.raises(ValueError, match='shape differs'):
        launcher.build_readonly_admission(b'print("not a pinned launcher")')


def test_admission_proofs_require_exact_readonly_version(args):
    expected = json.loads(args['preflight_raw'])['expected']
    value = {'mode': 'read-only', 'stage_verified': True, 'gws_verified': False,
             'object_verified': False, 'retention_authorized': False, 'eviction_authorized': False,
             'gws_absent': True, 'holds_ok': True, 'capacity_ok': True,
             'source_proof': {**expected, 'stable': True},
             'stage_proof': {'schema': 'radar-direct-gws-stage-verified-v1',
                             'size': expected['size'], 'sha256': expected['sha256'],
                             'retention_authorized': False, 'eviction_authorized': False}}
    assert launcher.admission_proofs(json.dumps(value).encode(), expected)['holds_ok'] is True
    value['source_proof']['sha256'] = 'f' * 64
    with pytest.raises(ValueError, match='version differs'):
        launcher.admission_proofs(json.dumps(value).encode(), expected)
    value['source_proof']['sha256'] = expected['sha256']
    value['gws_absent'] = None
    with pytest.raises(ValueError, match='admission failed'):
        launcher.admission_proofs(json.dumps(value).encode(), expected)


def test_capture_binds_new_unit_and_original_journal(args):
    template = (root / 'coordinator/capture_recovered_gws_103414.py').read_bytes()
    capture = launcher.build_capture(template=template, template_sha256=hashlib.sha256(template).hexdigest(),
        original_stamp=args['original_stamp'], preflight_raw=args['preflight_raw'], journal_raw=args['journal_raw'],
        operation_id=args['operation_id'], attempt=1).decode()
    assert 'aurora-direct-retry-' + args['operation_id'] + '-000001.service' in capture
    assert 'transfer-20261027-gws-only-20261111t153440z-0.jsonl' in capture
    assert '/retry-' + args['operation_id'] + '-000001/launcher.py' in capture
    assert 'out.mkdir(mode=0o700, exist_ok=True)' in capture
