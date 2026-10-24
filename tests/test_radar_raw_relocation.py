"""Exercise lossless copy and failure gates using small disposable raw days."""
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('relocate', ROOT / 'roles/radar_raw_relocation/files/aurora-radar-relocation.py')
relocate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relocate)


@pytest.fixture
def layout(tmp_path):
    plan = {'raw_root': str(tmp_path / 'project/raw'), 'destination_root': str(tmp_path / 'data/relocated'),
            'quarantine_root': str(tmp_path / 'project/recovery'), 'state_root': str(tmp_path / 'state'),
            'project_mount': str(tmp_path / 'project'), 'data_mount': str(tmp_path / 'data'),
            'days': ['Y2026/M05/D02'], 'expected_files_per_day': 2,
            'project_required_free_bytes': 100, 'data_reserve_bytes': 40, 'consumers': ['aurora-radar-append.service']}
    source, target, quarantine = relocate.paths(plan, plan['days'][0])
    source.mkdir(parents=True)
    Path(plan['data_mount']).mkdir()
    for name, content in [('one.LV0', b'raw native observation'), ('one.LV1.NC', b'netcdf observation')]:
        path = source / name
        path.write_bytes(content)
        os.utime(path, (1_700_000_000, 1_700_000_000))
    with ExitStack() as stack:
        stack.enter_context(patch.object(relocate, 'mount_roots'))
        stack.enter_context(patch.object(relocate, 'free_bytes', return_value=1000))
        # macOS Python lacks Linux xattr APIs; production and Linux CI exercise them.
        if not hasattr(os, 'listxattr'):
            stack.enter_context(patch.object(relocate.os, 'listxattr', return_value=[], create=True))
        yield plan, source, target, quarantine


def test_stage_keeps_originals_and_checks_bytes_metadata(layout):
    plan, source, target, _ = layout
    before = relocate.snapshot(source)
    result = relocate.stage(plan, plan['days'][0])
    assert result['files'] == 2
    assert relocate.snapshot(source) == before
    for name in before:
        assert (source / name).read_bytes() == (target / name).read_bytes()
        assert relocate.attributes(source / name) == relocate.attributes(target / name)
    assert relocate.verify_day(plan, plan['days'][0])['source_signature'] == before
    assert relocate.stage(plan, plan['days'][0]) == result  # safe verified resume


@pytest.mark.parametrize('damage', ['bytes', 'metadata', 'extra'])
def test_corrupt_or_changed_destination_blocks_verification(layout, damage):
    plan, _, target, _ = layout
    relocate.stage(plan, plan['days'][0])
    if damage == 'bytes': (target / 'one.LV0').write_bytes(b'corrupt')
    elif damage == 'metadata': os.chmod(target / 'one.LV0', 0o600)
    else: (target / 'unknown').write_text('keep')
    with pytest.raises(RuntimeError, match='mismatch|file set'):
        relocate.verify_day(plan, plan['days'][0])


def test_source_change_during_copy_retains_both_files(layout):
    plan, source, target, _ = layout
    original = relocate.hash_file
    calls = 0
    def change_after_copy(path):
        nonlocal calls
        result = original(path)
        calls += 1
        if calls == 2: (source / 'one.LV1.NC').write_bytes(b'changed after snapshot')
        return result
    with patch.object(relocate, 'hash_file', side_effect=change_after_copy):
        with pytest.raises(RuntimeError, match='Source changed'):
            relocate.stage(plan, plan['days'][0])
    assert (source / 'one.LV0').exists() and (target / 'one.LV0').exists()
    assert not relocate.manifest_path(plan, plan['days'][0]).exists()


def test_symlinks_hardlinks_recent_and_nested_files_are_rejected(layout):
    plan, source, _, _ = layout
    path = source / 'unsafe'
    for create in (lambda: path.symlink_to(source / 'one.LV0'),
                   lambda: os.link(source / 'one.LV0', path), lambda: path.mkdir(),
                   lambda: path.write_text('recent')):
        create()
        with pytest.raises(RuntimeError, match='ordinary|closed'):
            relocate.snapshot(source)
        if path.is_dir() and not path.is_symlink(): path.rmdir()
        else: path.unlink()


def test_both_volume_reserves_are_required(layout):
    plan, _, _, _ = layout
    with patch.object(relocate, 'free_bytes', side_effect=[50, 1000]):
        with pytest.raises(RuntimeError, match='seven-day'):
            relocate.capacity(plan, remaining_copy=80, reclaimable=49)
    with patch.object(relocate, 'free_bytes', side_effect=[1000, 119]):
        with pytest.raises(RuntimeError, match='Data capacity'):
            relocate.capacity(plan, remaining_copy=80, reclaimable=0)
    with patch.object(relocate, 'free_bytes', side_effect=[50, 120]):
        assert relocate.capacity(plan, remaining_copy=80, reclaimable=50)['data_free'] == 120


def test_missing_nested_mount_and_wrong_target_fail_closed(layout):
    plan, source, target, _ = layout
    target.mkdir(parents=True)
    with patch.object(relocate.os.path, 'ismount', return_value=False):
        with pytest.raises(RuntimeError, match='not mounted'):
            relocate.guard(plan)
    with patch.object(relocate.os.path, 'ismount', return_value=True):
        with pytest.raises(RuntimeError, match='not mounted'):
            relocate.guard(plan)


def test_release_never_unlinks_on_hash_or_graph_failure(layout):
    plan, source, target, quarantine = layout
    relocate.stage(plan, plan['days'][0])
    quarantine.parent.mkdir(parents=True)
    source.rename(quarantine)
    source.mkdir()
    with patch.object(relocate, 'assert_paused'), patch.object(relocate, 'guard'), patch.object(relocate, 'inspect'):
        with patch.object(relocate, 'graph', side_effect=RuntimeError('bad graph')):
            with pytest.raises(RuntimeError, match='bad graph'): relocate.release(plan, plan['days'][0])
        with patch.object(relocate, 'graph'):
            (target / 'one.LV0').write_bytes(b'corrupt')
            with pytest.raises(RuntimeError, match='Destination changed'): relocate.release(plan, plan['days'][0])
    assert len(list(quarantine.iterdir())) == 2


def test_release_removes_only_verified_quarantine_after_durable_record(layout):
    plan, source, target, quarantine = layout
    relocate.stage(plan, plan['days'][0])
    quarantine.parent.mkdir(parents=True)
    source.rename(quarantine)
    source.mkdir()
    with patch.object(relocate, 'assert_paused'), patch.object(relocate, 'guard'), patch.object(relocate, 'inspect'), patch.object(relocate, 'graph'):
        relocate.release(plan, plan['days'][0])
    assert not quarantine.exists()
    assert len(list(target.iterdir())) == 2
    assert list(Path(plan['state_root']).glob('*-release.json'))


def test_failed_evidence_fsync_keeps_all_originals(layout):
    plan, source, _, quarantine = layout
    relocate.stage(plan, plan['days'][0])
    quarantine.parent.mkdir(parents=True)
    source.rename(quarantine)
    source.mkdir()
    with patch.object(relocate, 'assert_paused'), patch.object(relocate, 'guard'), patch.object(relocate, 'inspect'), patch.object(relocate, 'graph'), patch.object(relocate, 'save', side_effect=OSError('disk failure')):
        with pytest.raises(OSError, match='disk failure'): relocate.release(plan, plan['days'][0])
    assert len(list(quarantine.iterdir())) == 2


def test_prepare_never_starts_mounts_or_services_and_plan_preserves_paths():
    tasks = yaml.safe_load((ROOT / 'roles/radar_raw_relocation/tasks/main.yml').read_text())
    assert not any('ansible.builtin.systemd' in task or 'ansible.builtin.systemd_service' in task for task in tasks)
    plan = yaml.safe_load((ROOT / 'inventory/radar-relocation-20261024.example.yml').read_text())['aurora_radar_relocation_plan']
    assert len(plan['days']) == 4 and plan['expected_files_per_day'] == 96
    assert plan['data_reserve_bytes'] == 40 * relocate.GIB
    unit = relocate.unit_text('/project/aurora/raw/rpgfmcw94/Y2026/M05/D02', '/data/aurora/raw-relocated/rpgfmcw94/Y2026/M05/D02')
    assert 'Requires=project.mount data.mount' in unit
    assert 'After=project.mount data.mount' in unit
    assert 'BindsTo=project.mount data.mount' in unit
    assert 'Options=bind,ro' in unit
    assert 'AssertPathIsDirectory=/data/aurora/raw-relocated/rpgfmcw94/Y2026/M05/D02' in unit


def test_config_rejects_traversal_and_overlapping_roots(layout, tmp_path):
    plan, _, _, _ = layout
    config = tmp_path / 'plan.json'
    config.write_text(json.dumps({**plan, 'days': ['../../outside']}))
    with pytest.raises(ValueError, match='day path'): relocate.load(config)
    config.write_text(json.dumps({**plan, 'destination_root': plan['raw_root'] + '/inside'}))
    with pytest.raises(ValueError, match='disjoint'): relocate.load(config)


def test_cutover_keeps_quarantine_and_rollback_restores_it(layout, tmp_path):
    plan, source, target, quarantine = layout
    original = {p.name: p.read_bytes() for p in source.iterdir()}
    relocate.stage(plan, plan['days'][0])
    with patch.object(relocate, 'ACTIVE', tmp_path / 'active.json'), patch.object(relocate, 'SYSTEMD_ROOT', tmp_path / 'systemd'), patch.object(relocate, 'unit_name', return_value='radar-day.mount'), patch.object(relocate, 'assert_paused'), patch.object(relocate, 'guard'), patch.object(relocate.subprocess, 'run'):
        relocate.cutover(plan)
        assert not list(source.iterdir())
        assert {p.name: p.read_bytes() for p in quarantine.iterdir()} == original
        assert relocate.ACTIVE.exists()
        relocate.rollback(plan)
        assert {p.name: p.read_bytes() for p in source.iterdir()} == original
        assert {p.name: p.read_bytes() for p in target.iterdir()} == original
        assert not quarantine.exists() and not relocate.ACTIVE.exists()


def test_mount_start_failure_preserves_originals_for_rollback(layout, tmp_path):
    plan, source, target, quarantine = layout
    relocate.stage(plan, plan['days'][0])
    with patch.object(relocate, 'ACTIVE', tmp_path / 'active.json'), patch.object(relocate, 'SYSTEMD_ROOT', tmp_path / 'systemd'), patch.object(relocate, 'unit_name', return_value='radar-day.mount'), patch.object(relocate, 'assert_paused'), patch.object(relocate.subprocess, 'run', side_effect=OSError('mount failed')):
        with pytest.raises(OSError, match='mount failed'): relocate.cutover(plan)
        assert len(list(quarantine.iterdir())) == 2 and len(list(target.iterdir())) == 2
        assert relocate.ACTIVE.exists()
        with pytest.raises(RuntimeError, match='not mounted'): relocate.guard(plan)


def test_active_or_queued_consumer_blocks_cutover(layout):
    plan, _, _, _ = layout
    for state in ('LoadState=loaded\nActiveState=active\nMainPID=0\nJob=0',
                  'LoadState=loaded\nActiveState=inactive\nMainPID=0\nJob=123'):
        with patch.object(relocate.subprocess, 'check_output', return_value=state):
            with pytest.raises(RuntimeError, match='Pause and drain'): relocate.assert_paused(plan)


def test_missing_dependency_blocks_original_release(layout):
    plan, _, _, _ = layout
    with patch.object(relocate, 'unit_name', return_value='radar-day.mount'), patch.object(relocate.subprocess, 'check_output', return_value='Requires=radar-day.mount\nAfter=radar-day.mount\nBindsTo=\nExecStartPre='):
        with pytest.raises(RuntimeError, match='dependencies/guard'): relocate.graph(plan)


def test_cutover_checks_signatures_without_long_rehash(layout, tmp_path):
    plan, source, target, _ = layout
    relocate.stage(plan, plan['days'][0])
    with patch.object(relocate, 'hash_file', side_effect=AssertionError('long rehash in pause')):
        relocate.verify_fast(plan, plan['days'][0])
        (target / 'one.LV0').write_bytes(b'changed after full verification')
        with pytest.raises(RuntimeError, match='Destination changed'):
            relocate.verify_fast(plan, plan['days'][0])
    (source / 'one.LV1.NC').write_bytes(b'new original bytes')
    with pytest.raises(RuntimeError, match='Original changed'):
        relocate.verify_fast(plan, plan['days'][0])


def test_interrupted_release_resumes_from_durable_per_file_intent(layout):
    plan, source, target, quarantine = layout
    relocate.stage(plan, plan['days'][0])
    quarantine.parent.mkdir(parents=True)
    source.rename(quarantine)
    source.mkdir()
    original_save = relocate.save
    def fail_after_first_unlink(path, value):
        if str(path).endswith('-release.json') and value.get('released'):
            raise OSError('power loss after unlink before progress commit')
        original_save(path, value)
    with patch.object(relocate, 'assert_paused'), patch.object(relocate, 'guard'), patch.object(relocate, 'inspect'), patch.object(relocate, 'graph'):
        with patch.object(relocate, 'save', side_effect=fail_after_first_unlink):
            with pytest.raises(OSError, match='power loss'): relocate.release(plan, plan['days'][0])
        assert len(list(quarantine.iterdir())) == 1
        state = json.loads(relocate.progress_path(plan, plan['days'][0]).read_text())
        assert state['pending'] and not state['released']
        with pytest.raises(RuntimeError, match='release has started'): relocate.rollback(plan)
        # Full re-verification of remaining originals and all target bytes is resumable.
        relocate.verify_day(plan, plan['days'][0], mounted=True)
        relocate.release(plan, plan['days'][0])
    assert not quarantine.exists() and len(list(target.iterdir())) == 2
    assert len(json.loads(relocate.progress_path(plan, plan['days'][0]).read_text())['released']) == 2


def test_active_record_is_readable_even_with_restrictive_operator_umask(tmp_path):
    previous = os.umask(0o077)
    try: relocate.save(tmp_path / 'active.json', {'plan': 'readable to remote guard'})
    finally: os.umask(previous)
    assert (tmp_path / 'active.json').stat().st_mode & 0o777 == 0o644


def test_guard_requires_read_only_bind_and_data_reserve(layout):
    plan, source, target, _ = layout
    relocate.stage(plan, plan['days'][0])
    with patch.object(relocate.os.path, 'ismount', side_effect=lambda p: Path(p) == source), patch.object(Path, 'samefile', return_value=True):
        with patch.object(relocate.subprocess, 'check_output', return_value='rw,relatime'):
            with pytest.raises(RuntimeError, match='read-only'): relocate.guard(plan)
        with patch.object(relocate.subprocess, 'check_output', return_value='ro,relatime'):
            relocate.guard(plan)
            with pytest.raises(RuntimeError, match='reserve'): relocate.guard(plan, additional_bytes=961)


@pytest.mark.parametrize('damage', ['absent', 'empty', 'missing', 'changed', 'extra', 'manifest'])
def test_mounted_destination_requires_original_verified_file_set(layout, damage):
    plan, source, target, _ = layout
    relocate.stage(plan, plan['days'][0])
    if damage in ('absent', 'empty'):
        for path in target.iterdir(): path.unlink()
        if damage == 'absent': target.rmdir()
    elif damage == 'missing': (target / 'one.LV0').unlink()
    elif damage == 'changed':
        path = target / 'one.LV0'
        before = path.stat()
        path.write_bytes(b'x' * before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif damage == 'extra': (target / 'unknown').write_text('unexpected')
    else: relocate.manifest_path(plan, plan['days'][0]).unlink()
    with patch.object(relocate.os.path, 'ismount', side_effect=lambda p: Path(p) == source), patch.object(Path, 'samefile', return_value=True), patch.object(relocate.subprocess, 'check_output', return_value='ro,relatime'), patch.object(relocate, 'hash_file', side_effect=AssertionError('guard must not rehash bytes')):
        with pytest.raises((RuntimeError, FileNotFoundError)):
            relocate.guard(plan)


def test_unchanged_guard_checks_metadata_without_rehash(layout):
    plan, source, _, _ = layout
    relocate.stage(plan, plan['days'][0])
    with patch.object(relocate.os.path, 'ismount', side_effect=lambda p: Path(p) == source), patch.object(Path, 'samefile', return_value=True), patch.object(relocate.subprocess, 'check_output', return_value='ro,relatime'), patch.object(relocate, 'hash_file', side_effect=AssertionError('guard must not rehash bytes')):
        relocate.guard(plan)


def test_graph_accepts_systemctl_quoted_escaped_mount_names(layout):
    plan, _, _, _ = layout
    name = r'project-aurora-recovery-probe\x2dwith\x2dhyphens.mount'
    displayed = json.dumps(name)
    properties = '\n'.join(prop + '=other.service ' + displayed for prop in ('Requires', 'After', 'BindsTo'))
    properties += '\nExecStartPre={ path=' + relocate.HELPER + ' ; argv[]=guard ; }'
    with patch.object(relocate, 'unit_name', return_value=name), patch.object(relocate.subprocess, 'check_output', return_value=properties):
        assert relocate.graph(plan) == {'consumers': 1, 'mounts': 1}


def test_remote_exec_parses_options_and_keeps_lock_until_exec(tmp_path):
    lock = tmp_path / 'lock'; lock.touch()
    with patch.object(relocate, 'LOCK', lock), patch.object(relocate.sys, 'argv', ['helper', 'exec', '--if-active', '--config', str(tmp_path / 'inactive.json'), '--', '/bin/bash', '-c', 'rsync --server . /raw']), patch.object(relocate.os, 'execvp') as execute:
        assert relocate.main() == 0
        execute.assert_called_once_with('/bin/bash', ['/bin/bash', '-c', 'rsync --server . /raw'])


@pytest.mark.parametrize('action', ['stage', 'verify'])
def test_late_change_to_already_hashed_destination_never_becomes_valid_proof(layout, action):
    plan, _, target, _ = layout
    if action == 'verify': relocate.stage(plan, plan['days'][0])
    original = relocate.hash_file
    old_proof = relocate.manifest_path(plan, plan['days'][0]).read_bytes() if action == 'verify' else None
    def mutate_prior_target_after_last_hash(path):
        result = original(path)
        if path == target / 'one.LV1.NC':
            previous = target / 'one.LV0'
            old_time = previous.stat().st_mtime_ns
            previous.write_bytes(b'changed after its own hash')
            os.utime(previous, ns=(old_time, old_time))
        return result
    with patch.object(relocate, 'hash_file', side_effect=mutate_prior_target_after_last_hash):
        with pytest.raises(RuntimeError, match='Destination changed'):
            if action == 'stage': relocate.stage(plan, plan['days'][0])
            else: relocate.verify_day(plan, plan['days'][0])
    proof = relocate.manifest_path(plan, plan['days'][0])
    if old_proof is None: assert not proof.exists()
    else: assert proof.read_bytes() == old_proof
