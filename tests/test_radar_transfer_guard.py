import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment, StrictUndefined
import pytest
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('radar_guard', ROOT / 'roles/source_sync/files/aurora-radar-transfer-guard.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


@pytest.mark.parametrize('available,allowed', [(24, True), (23, False)])
def test_reserve_covers_four_simultaneous_largest_files(tmp_path, available, allowed):
    with patch.object(guard.os.path, 'ismount', return_value=True), patch.object(guard.os, 'statvfs', return_value=SimpleNamespace(f_bavail=available, f_frsize=guard.GIB)):
        if allowed:
            assert guard.check_capacity(tmp_path, tmp_path, 4, 4 * guard.GIB, 8 * guard.GIB)['required_bytes'] == 24 * guard.GIB
        else:
            with pytest.raises(RuntimeError, match='capacity hold'):
                guard.check_capacity(tmp_path, tmp_path, 4, 4 * guard.GIB, 8 * guard.GIB)


def test_missing_mount_and_symlink_partial_root_fail_closed(tmp_path):
    with patch.object(guard.os.path, 'ismount', return_value=False):
        with pytest.raises(RuntimeError, match='mount is missing'):
            guard.check_capacity(tmp_path, tmp_path, 4, 1, 8)
    (tmp_path / '.radar-partials').symlink_to(tmp_path)
    with patch.object(guard.os.path, 'ismount', return_value=True):
        with pytest.raises(RuntimeError, match='same mounted filesystem'):
            guard.check_capacity(tmp_path, tmp_path, 4, 1, 8)


def files(tmp_path, content=b'abc', age=30000, incoming=False):
    target = tmp_path / 'Y2026/M10/data.LV0'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'abcdef')
    partial = tmp_path / '.radar-partials/0/Y2026/M10/data.LV0'
    if incoming:
        partial = partial.with_name(partial.name + '.incoming.123')
    partial.parent.mkdir(parents=True)
    partial.write_bytes(content)
    os.utime(partial, (guard.time.time() - age, guard.time.time() - age))
    return partial, target, tmp_path / 'audit.jsonl'


@pytest.mark.parametrize('incoming', [False, True])
def test_only_verified_duplicate_partial_is_removed_with_durable_audit(tmp_path, incoming):
    partial, target, audit = files(tmp_path, incoming=incoming)
    assert guard.retire_duplicates(tmp_path, 'Y2026/M10/data.LV0', 4, audit) == 3
    assert not partial.exists()
    assert target.read_bytes() == b'abcdef'
    evidence = [json.loads(row) for row in audit.read_text().splitlines()]
    assert [row['action'] for row in evidence] == ['verified', 'removed_partial']
    assert evidence[0]['prefix_sha256'] == guard.hashlib.sha256(b'abc').hexdigest()


@pytest.mark.parametrize('content,age', [(b'xyz', 30000), (b'abc', 10), (b'abcdefgh', 30000)])
def test_mismatch_recent_or_larger_partial_is_preserved(tmp_path, content, age):
    partial, target, audit = files(tmp_path, content=content, age=age)
    assert guard.retire_duplicates(tmp_path, 'Y2026/M10/data.LV0', 4, audit) == 0
    assert partial.read_bytes() == content
    assert target.read_bytes() == b'abcdef'
    assert not audit.exists()


def test_durability_failure_prevents_unlink(tmp_path):
    partial, target, audit = files(tmp_path)
    with patch.object(guard.os, 'fsync', side_effect=OSError('storage error')):
        with pytest.raises(OSError, match='storage error'):
            guard.retire_duplicates(tmp_path, 'Y2026/M10/data.LV0', 4, audit)
    assert partial.exists() and target.exists()


def test_containment_and_symlinks_are_protected(tmp_path):
    partial, target, audit = files(tmp_path)
    with pytest.raises(ValueError, match='relative path'):
        guard.retire_duplicates(tmp_path, '../outside', 4, audit)
    partial.unlink()
    partial.symlink_to(target)
    assert guard.retire_duplicates(tmp_path, 'Y2026/M10/data.LV0', 4, audit) == 0
    assert partial.is_symlink()
    assert target.read_bytes() == b'abcdef'


def test_backfill_checks_capacity_before_every_transfer_and_preserves_retries():
    env = Environment(undefined=StrictUndefined)
    env.filters['quote'] = lambda value: shlex.quote(str(value))
    template = (ROOT / 'roles/source_sync/templates/aurora-radar-backfill.j2').read_text()
    rendered = env.from_string(template).render(
        radar_source_user='aurora', radar_source_host='source', radar_source_port=22,
        radar_source_path='/source', radar_source_pattern='*', radar_source_destination='/project/aurora/raw/rpgfmcw94',
        radar_backfill_workers=4, radar_backfill_batch_files=24, radar_backfill_batch_bytes=80*guard.GIB,
        radar_backfill_partial_root='/project/aurora/raw/rpgfmcw94/.radar-partials',
        radar_backfill_status_file='/var/lib/aurora-cloud/radar-backfill.json', radar_service_user='aurora',
        aurora_service_user='aurora', aurora_state_root='/var/lib/aurora-cloud',
        radar_source_required_mount='/project', radar_backfill_free_reserve_bytes=8*guard.GIB,
        radar_backfill_partial_audit='/var/lib/aurora-cloud/radar-partials.jsonl')
    subprocess.run(['bash', '-n'], input=rendered, text=True, check=True)
    worker = rendered.split('transfer_worker() {', 1)[1]
    assert worker.index('"$transfer_guard" capacity') < worker.index('mktemp') < worker.index('if ! ssh')
    assert '--max-file-bytes "$largest_file_bytes"' in worker
    assert worker.index('mv -f "$partial" "$target"') < worker.index('retire-duplicates')
    assert '${partial}.incoming.XXXXXX' in worker
    assert rendered.index('"$transfer_guard" capacity') < rendered.index('mkdir -p "$destination"')


def test_target_change_after_audit_keeps_partial(tmp_path):
    partial, target, audit = files(tmp_path)
    original_fsync = guard.os.fsync
    calls = 0

    def fsync_then_change(descriptor):
        nonlocal calls
        original_fsync(descriptor)
        calls += 1
        if calls == 2:
            target.write_bytes(b'changed after verification')

    with patch.object(guard.os, 'fsync', side_effect=fsync_then_change):
        assert guard.retire_duplicates(tmp_path, 'Y2026/M10/data.LV0', 4, audit) == 0
    assert partial.read_bytes() == b'abc'
    assert all(json.loads(row)['action'] != 'removed_partial' for row in audit.read_text().splitlines())
