import json
import base64
import gzip
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
import windows_native_probe as probe

SOURCE = 'Y2026/M11/D13/rpgfmcw94-aurora_261113_215959_P01_ZEN.LV1'
NEXT = 'Y2026/M11/D13/rpgfmcw94-aurora_261113_225959_P01_ZEN.LV1'


def test_successor_is_later_same_programme_and_day_consistent():
    probe.validate_pair(SOURCE, NEXT)
    with pytest.raises(ValueError, match='successor'):
        probe.validate_pair(SOURCE, NEXT.replace('.LV1', '.LV0'))
    with pytest.raises(ValueError, match='successor'):
        probe.validate_pair(NEXT, SOURCE)
    with pytest.raises(ValueError, match='directory date'):
        probe.validate_pair(SOURCE, NEXT.replace('/D13/', '/D12/'))
    with pytest.raises(ValueError, match='native radar path'):
        probe.validate_pair(SOURCE + "';Remove-Item C:\\", NEXT)


def test_task_encoding_has_a_platform_independent_gzip_header():
    wrapper = probe.ps_wrapper('example script')
    payload = re.search(r"FromBase64String\('([^']+)'\)", wrapper).group(1)
    raw = base64.b64decode(payload)
    assert raw[9] == 255 and gzip.decompress(raw) == b'example script'


def test_reservation_survives_unknown_launch_and_blocks_duplicates(tmp_path):
    directory, record = probe.reserve(tmp_path, SOURCE, NEXT)
    assert probe.read_state(directory / 'intent.json') == record
    assert len(probe.task_parts(record)[1]) < 7000
    with pytest.raises(ValueError, match='requires reconciliation'):
        probe.reserve(tmp_path, SOURCE, NEXT)
    probe.durable_write(directory / 'complete.json', b'{"cleanup_confirmed":true}\n')
    directory2, _ = probe.reserve(tmp_path, SOURCE, NEXT)
    assert directory2 != directory


def test_receipt_must_bind_exact_operation_and_version(tmp_path, monkeypatch):
    _, record = probe.reserve(tmp_path, SOURCE, NEXT)
    proof = {'schema': 'radar-windows-observation-v1', 'operation': record['operation'],
             'source': SOURCE, 'successor': NEXT, 'size': 123, 'mtime_ticks': 123,
             'successor_size': 456, 'successor_mtime_ticks': 456,
             'observed_at': 1794610000, 'sha256': 'a' * 64, 'ok': True}
    monkeypatch.setattr(probe, 'powershell', lambda script, jump: {
        'task_exists': True, 'state': 'Ready', 'receipt': json.dumps(proof),
        'principal': 'aurora', 'logon_type': 'Interactive', 'run_level': 'Limited',
        'last_result': 0, 'last_run_utc': '1970-01-01T00:00:00Z',
        'execution_time_limit': 'PT20M', 'multiple_instances': 'IgnoreNew'})
    observed, attestation = probe.poll(record, jump=True)
    assert observed == proof
    probe.validate_attestation(record, observed, attestation)
    with pytest.raises(ValueError, match='attestation'):
        probe.validate_attestation(record, dict(observed, size=124), attestation)
    with pytest.raises(RuntimeError, match='exact source'):
        probe.validate_proof(record, dict(proof, successor=SOURCE))
    monkeypatch.setattr(probe, 'powershell', lambda script, jump: {'task_exists': False})
    with pytest.raises(RuntimeError, match='missing'):
        probe.poll(record, jump=True)


def test_running_or_not_yet_started_task_never_causes_relaunch(tmp_path, monkeypatch):
    _, record = probe.reserve(tmp_path, SOURCE, NEXT)
    for state in ('Ready', 'Running'):
        monkeypatch.setattr(probe, 'powershell', lambda script, jump, state=state: {
            'task_exists': True, 'state': state, 'receipt': None, 'last_result': 267011})
        assert probe.poll(record, jump=True) is None


def test_finished_task_without_receipt_is_unresolved(tmp_path, monkeypatch):
    _, record = probe.reserve(tmp_path, SOURCE, NEXT)
    monkeypatch.setattr(probe, 'powershell', lambda script, jump: {
        'task_exists': True, 'state': 'Ready', 'receipt': None, 'last_result': 1})
    with pytest.raises(RuntimeError, match='finished without a receipt'):
        probe.poll(record, jump=True)
