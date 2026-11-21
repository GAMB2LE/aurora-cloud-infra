import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'))
from stage_native_version import stage
from inspect_protected_stage import inspect
from windows_observation_gate import WINDOWS_UNIX_EPOCH_TICKS, staging_eligibility_from_captures
from windows_native_probe import job_script, task_parts

SOURCE = 'Y2026/M11/D13/rpgfmcw94-aurora_261113_215959_P01_ZEN.LV1'
NEXT = 'Y2026/M11/D13/rpgfmcw94-aurora_261113_225959_P01_ZEN.LV1'


def fixture(tmp_path):
    source_root, stage_root = tmp_path / 'source', tmp_path / 'stage'
    path = source_root / SOURCE
    path.parent.mkdir(parents=True)
    path.write_bytes(b'native radar sample')
    ns = 1_794_600_000_000_000_000
    os.utime(path, ns=(ns, ns))
    stage_root.mkdir(mode=0o700)
    now = 1_794_605_000
    def capture(letter, when):
        operation = letter * 32
        proof = {'schema': 'radar-windows-observation-v1', 'operation': operation, 'ok': True,
                 'source': SOURCE, 'successor': NEXT, 'size': path.stat().st_size,
                 'mtime_ticks': WINDOWS_UNIX_EPOCH_TICKS + path.stat().st_mtime_ns // 100,
                 'successor_size': 1, 'successor_mtime_ticks': WINDOWS_UNIX_EPOCH_TICKS + path.stat().st_mtime_ns // 100,
                 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'observed_at': when}
        intent = {'schema': 'radar-windows-probe-intent-v1', 'operation': operation,
                  'source': SOURCE, 'successor': NEXT,
                  'job_sha256': hashlib.sha256(job_script(operation, SOURCE, NEXT).encode()).hexdigest(),
                  'retention_authorized': False, 'eviction_authorized': False}
        name, arguments, _ = task_parts(intent)
        attestation = {'schema': 'radar-windows-task-attestation-v1', 'operation': operation,
                       'task': name, 'job_sha256': intent['job_sha256'],
                       'arguments_sha256': hashlib.sha256(arguments.encode()).hexdigest(),
                       'principal': 'aurora', 'logon_type': 'Interactive', 'run_level': 'Limited',
                       'last_result': 0, 'last_run_utc': '1970-01-01T00:00:00Z',
                       'receipt_sha256': hashlib.sha256(json.dumps(proof, sort_keys=True,
                           separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()}
        return {'intent': intent, 'attestation': attestation, 'observation': proof,
                'complete': {'cleanup_confirmed': True}}
    captures = [capture('a', now - 700), capture('b', now - 100)]
    value = staging_eligibility_from_captures(*captures, now=now)
    return source_root, stage_root, path, value, captures, now


def test_staging_is_no_overwrite_and_does_not_authorize_fast_archive(tmp_path):
    root, destination, source, eligibility, captures, now = fixture(tmp_path)
    proof = stage(root, destination, eligibility, 'c' * 32, captures=captures, minimum_free_bytes=0, now=now+1)
    staged = Path(proof['stage_path'])
    assert staged.read_bytes() == source.read_bytes()
    assert proof['payloads_validated'] is proof['fast_archive_authorized'] is False
    assert proof['retention_authorized'] is proof['eviction_authorized'] is False
    observed, readback = inspect(destination, 'c' * 32, owner=os.getuid())
    assert observed == proof and readback['sha256'] == eligibility['sha256']
    staged.chmod(0o640)
    with pytest.raises(ValueError, match='protected recorded version'):
        inspect(destination, 'c' * 32, owner=os.getuid())
    staged.chmod(0o440)
    with pytest.raises(FileExistsError):
        stage(root, destination, eligibility, 'c' * 32, captures=captures, minimum_free_bytes=0, now=now+1)
    assert staged.read_bytes() == source.read_bytes()


def test_mismatched_content_retains_partial_without_publishing(tmp_path):
    root, destination, source, eligibility, captures, now = fixture(tmp_path)
    source.write_bytes(b'native radar samplE')
    os.utime(source, ns=((eligibility['mtime_ticks'] - WINDOWS_UNIX_EPOCH_TICKS) * 100,)*2)
    with pytest.raises(ValueError, match='checksum differs'):
        stage(root, destination, eligibility, 'd' * 32, captures=captures, minimum_free_bytes=0, now=now+1)
    operation = destination / ('d' * 32)
    assert (operation / 'intent.json').is_file()
    assert (operation / ('.' + source.name + '.partial')).is_file()
    assert not (operation / source.name).exists()


def test_stale_eligibility_and_symlink_source_are_refused(tmp_path):
    root, destination, source, eligibility, captures, now = fixture(tmp_path)
    with pytest.raises(ValueError, match='bound to authenticated captures'):
        stage(root, destination, dict(eligibility, sha256='f' * 64), 'e' * 32,
              captures=captures, minimum_free_bytes=0, now=now+1)
    with pytest.raises(ValueError, match='stale'):
        stage(root, destination, eligibility, 'e' * 32, captures=captures, minimum_free_bytes=0, now=now+301)
    actual = source.with_name('other')
    source.rename(actual)
    source.symlink_to(actual)
    with pytest.raises(OSError):
        stage(root, destination, eligibility, 'e' * 32, captures=captures, minimum_free_bytes=0, now=now+1)
    assert not (destination / ('e' * 32)).exists()
