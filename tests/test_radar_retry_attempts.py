import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('retry_attempts', Path(__file__).parents[1] /
    'roles/radar_archive_recovery/files/retry_attempts.py')
attempts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(attempts)


def test_restart_adopts_same_attempt_but_rejects_changed_inputs(tmp_path):
    op = 'a' * 32
    proof = {'state': 'precommit-lock-refusal', 'invocation_id': 'b' * 32,
             'journal_sha256': 'c' * 64, 'expected': {'relative_path': 'test.LV1'}}
    inputs = {key: key.encode() for key in ('continuity', 'observation', 'request', 'launcher')}
    intent = attempts.reserve(tmp_path, op, 1, proof, inputs, 'gzip-v3-vm03')
    assert attempts.reserve(tmp_path, op, 1, proof, inputs, 'gzip-v3-vm03') == intent
    changed = {**inputs, 'continuity': b'different'}
    with pytest.raises(ValueError, match='immutable'):
        attempts.reserve(tmp_path, op, 1, proof, changed, 'gzip-v3-vm03')
    directory = tmp_path / (op + '-000001')
    identity = {'unit': intent['unit'], 'invocation_id': 'd' * 32, 'main_pid': 42}
    attempts.record_identity(directory, identity)
    attempts.record_identity(directory, identity)
    assert attempts.load(directory) == (intent, inputs, identity)
    with pytest.raises(ValueError, match='immutable'):
        attempts.record_identity(directory, {**identity, 'invocation_id': 'e' * 32})


def test_corrupt_persisted_inputs_cannot_be_reused(tmp_path):
    op = 'a' * 32
    proof = {'state': 'precommit-lock-refusal'}
    inputs = {key: key.encode() for key in ('continuity', 'observation', 'request', 'launcher')}
    attempts.reserve(tmp_path, op, 1, proof, inputs, 'release')
    directory = tmp_path / (op + '-000001')
    (directory / 'request.json').write_text('{"hex":"00","sha256":"bad"}')
    with pytest.raises(ValueError, match='checksum'):
        attempts.load(directory)


def test_unknown_failure_and_symlink_are_not_adopted(tmp_path):
    op = 'a' * 32
    inputs = {key: key.encode() for key in ('continuity', 'observation', 'request', 'launcher')}
    with pytest.raises(ValueError, match='precommit'):
        attempts.reserve(tmp_path, op, 1, {'state': 'unknown'}, inputs, 'release')
    (tmp_path / (op + '-000001')).symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='unsafe'):
        attempts.reserve(tmp_path, op, 1, {'state': 'precommit-lock-refusal'}, inputs, 'release')
