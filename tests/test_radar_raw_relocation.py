"""Exercise lossless copy and failure gates using small disposable raw days."""
import importlib.util
import fcntl
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack, contextmanager

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
    with simulated_bind(plan, source, target), patch.object(relocate, 'assert_paused'), patch.object(relocate, 'inspect'), patch.object(relocate, 'graph'):
        relocate.verify_day(plan, plan['days'][0], mounted=True)
        relocate.release(plan, plan['days'][0])
    assert not quarantine.exists()
    assert len(list(target.iterdir())) == 2
    assert list(Path(plan['state_root']).glob('*-release.json'))


def test_failed_evidence_fsync_keeps_all_originals(layout):
    plan, source, target, quarantine = layout
    relocate.stage(plan, plan['days'][0])
    quarantine.parent.mkdir(parents=True)
    source.rename(quarantine)
    source.mkdir()
    with simulated_bind(plan, source, target), patch.object(relocate, 'assert_paused'), patch.object(relocate, 'inspect'), patch.object(relocate, 'graph'):
        relocate.verify_day(plan, plan['days'][0], mounted=True)
        with patch.object(relocate, 'save', side_effect=OSError('disk failure')):
            with pytest.raises(OSError, match='disk failure'): relocate.release(plan, plan['days'][0])
    assert len(list(quarantine.iterdir())) == 2


def test_prepare_never_starts_mounts_or_services_and_plan_preserves_paths():
    tasks = yaml.safe_load((ROOT / 'roles/radar_raw_relocation/tasks/main.yml').read_text())
    assert all(set(task.get('ansible.builtin.systemd', {})) <= {'daemon_reload'} for task in tasks)
    helper = next(i for i,t in enumerate(tasks) if t['name'] == 'Install bounded relocation helper')
    config = next(i for i,t in enumerate(tasks) if t['name'] == 'Install reviewed relocation plan without activating it')
    assert helper < config  # Staging may load the next day between these tasks.
    assert not any(t.get('ansible.builtin.copy', {}).get('dest') == '/etc/aurora/radar-relocation.hold' for t in tasks)
    plan = yaml.safe_load((ROOT / 'inventory/radar-relocation-20261024.example.yml').read_text())['aurora_radar_relocation_plan']
    assert len(plan['days']) == 5 and plan['expected_files_per_day'] == 96
    assert plan['expected_files_by_day'] == {'Y2026/M05/D13': 64}
    assert plan['project_required_free_bytes'] == 617088629797 + 54622066409
    assert plan['data_reserve_bytes'] == 50 * relocate.GIB
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


def test_active_or_queued_consumer_blocks_cutover(layout, tmp_path):
    plan, _, _, _ = layout
    install_test_barrier(plan, tmp_path)
    for state in ('LoadState=loaded\nActiveState=active\nMainPID=0\nJob=0\nNeedDaemonReload=no',
                  'LoadState=loaded\nActiveState=inactive\nMainPID=0\nJob=123\nNeedDaemonReload=no'):
        with patch.object(relocate, 'SYSTEMD_ROOT', tmp_path / 'systemd'), patch.object(relocate.subprocess, 'check_output', return_value=state):
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
    with simulated_bind(plan, source, target), patch.object(relocate, 'assert_paused'), patch.object(relocate, 'inspect'), patch.object(relocate, 'graph'):
        relocate.verify_day(plan, plan['days'][0], mounted=True)
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


def test_remote_exec_parses_options_and_preserves_sender_status(tmp_path):
    lock = tmp_path / 'lock'; lock.touch()
    with patch.object(relocate, 'LOCK', lock), patch.object(relocate.sys, 'argv', ['helper', 'exec', '--if-active', '--config', str(tmp_path / 'inactive.json'), '--', '/bin/bash', '-c', 'rsync --server . /raw']), patch.object(relocate, 'run_sender', return_value=23) as execute:
        assert relocate.main() == 23
        execute.assert_called_once_with(['/bin/bash', '-c', 'rsync --server . /raw'])


def sender_process(lock, command, *, deny_signal=False):
    launcher = '''import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('helper',sys.argv[1])
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
helper.LOCK=Path(sys.argv[2])
if sys.argv[3]=='deny':
 def deny(*args):
  print('denied',flush=True)
  raise PermissionError('setuid sender')
 helper.os.killpg=deny
sys.argv=['helper','exec','--if-active','--config',str(helper.LOCK.parent/'inactive.json'),'--']+sys.argv[4:]
sys.exit(helper.main())
'''
    return subprocess.Popen([sys.executable, '-c', launcher, str(SPEC.origin), str(lock), 'deny' if deny_signal else 'allow', *command],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def assert_lock_held(lock):
    with lock.open('r') as stream:
        with pytest.raises(BlockingIOError):
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_remote_sender_closing_inherited_descriptors_cannot_release_lock(tmp_path):
    lock = tmp_path / 'lock'; lock.touch()
    child = "import os,sys;os.closerange(3,256);print('ready',flush=True);sys.stdin.readline();sys.exit(23)"
    process = sender_process(lock, [sys.executable, '-c', child])
    try:
        assert process.stdout.readline() == 'ready\n'
        assert_lock_held(lock)
        stdout, stderr = process.communicate('finish\n', timeout=10)
        assert process.returncode == 23
        assert stdout == stderr == ''  # No JSON/status text may enter the rsync protocol.
        with lock.open('r') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if process.poll() is None:
            process.communicate('finish\n', timeout=10)


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_sender_signal_forwarding_waits_for_cleanup_before_unlocking(tmp_path, signum):
    lock = tmp_path / 'lock'; lock.touch()
    child = '''import os,signal,sys
os.closerange(3,256)
def stop(signum,frame):
    print('cleaning',flush=True)
    sys.stdin.readline()
    sys.exit(19)
for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(sig,stop)
print('ready',flush=True)
signal.pause()
'''
    process = sender_process(lock, [sys.executable, '-c', child])
    try:
        assert process.stdout.readline() == 'ready\n'
        process.send_signal(signum)
        assert process.stdout.readline() == 'cleaning\n'
        assert_lock_held(lock)
        stdout, stderr = process.communicate('finish\n', timeout=10)
        assert process.returncode == 19
        assert stdout == stderr == ''
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.communicate('finish\n', timeout=10)


def test_sender_signal_exit_and_missing_command_are_reported(tmp_path):
    lock = tmp_path / 'lock'; lock.touch()
    process = sender_process(lock, [sys.executable, '-c', 'import os,signal;os.kill(os.getpid(),signal.SIGTERM)'])
    assert process.communicate(timeout=10) == ('', '')
    assert process.returncode == 128 + signal.SIGTERM
    process = sender_process(lock, [str(tmp_path / 'absent-command')])
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 75 and stdout == '' and 'No such file' in stderr


def test_denied_signal_keeps_supervising_until_sender_exits(tmp_path):
    lock = tmp_path / 'lock'; lock.touch()
    child = "import sys;print('ready',flush=True);sys.stdin.readline();sys.exit(0)"
    process = sender_process(lock, [sys.executable, '-c', child], deny_signal=True)
    try:
        assert process.stdout.readline() == 'ready\n'
        process.send_signal(signal.SIGTERM)
        assert process.stdout.readline() == 'denied\n'
        assert_lock_held(lock)
        stdout, stderr = process.communicate('finish\n', timeout=10)
        assert process.returncode == 128 + signal.SIGTERM
        assert stdout == stderr == ''
    finally:
        if process.poll() is None:
            process.communicate('finish\n', timeout=10)


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


def install_test_barrier(plan, tmp_path):
    plan['launch_hold'] = str(tmp_path / 'relocation.hold')
    plan['launchers'] = ['aurora-object-store-recovery.service']
    Path(plan['launch_hold']).write_text('owned test hold')
    for service in plan['consumers']:
        path = tmp_path / 'systemd' / (service + '.d') / '50-radar-relocation-hold.conf'
        path.parent.mkdir(parents=True)
        path.write_text(relocate.launch_hold_text(plan))


def test_launch_barrier_required_loaded_and_launcher_drained(layout, tmp_path):
    plan, _, _, _ = layout
    with pytest.raises(RuntimeError, match='Activate'):
        relocate.assert_paused(plan)
    install_test_barrier(plan, tmp_path)
    state = 'LoadState=loaded\nActiveState=inactive\nMainPID=0\nJob=0\nNeedDaemonReload=no'
    with patch.object(relocate, 'SYSTEMD_ROOT', tmp_path / 'systemd'):
        with patch.object(relocate.subprocess, 'check_output', return_value=state) as check:
            relocate.assert_paused(plan)
            assert any('aurora-object-store-recovery.timer' in call.args[0] for call in check.call_args_list)
        with patch.object(relocate.subprocess, 'check_output', return_value=state.replace('Reload=no', 'Reload=yes')):
            with pytest.raises(RuntimeError, match='Reload'):
                relocate.assert_paused(plan)
        barrier = tmp_path / 'systemd' / (plan['consumers'][0] + '.d') / '50-radar-relocation-hold.conf'
        barrier.write_text('[Unit]\n')
        with pytest.raises(RuntimeError, match='Missing or changed'):
            relocate.assert_paused(plan)


@pytest.mark.parametrize('unit', ['aurora-object-store-recovery-worker@products.service', 'aurora-worker@raw.service', 'aurora-worker@raw.service\nOther=bad'])
def test_only_explicit_raw_worker_instance_is_allowed(layout, tmp_path, unit):
    plan, _, _, _ = layout
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({**plan, 'consumers': [unit]}))
    with pytest.raises(ValueError, match='consumer'):
        relocate.load(config)
    config.write_text(json.dumps({**plan, 'consumers': ['aurora-object-store-recovery-worker@raw.service']}))
    assert relocate.load(config)['consumers'] == ['aurora-object-store-recovery-worker@raw.service']


@pytest.mark.parametrize('override', [{'Y2026/M05/D13': 64}, {'Y2026/M05/D02': 0}, {'Y2026/M05/D02': True}, {'Y2026/M05/D02': '2'}])
def test_count_override_requires_reviewed_day_and_positive_integer(layout, tmp_path, override):
    plan, _, _, _ = layout
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({**plan, 'expected_files_by_day': override}))
    with pytest.raises(ValueError, match='explicitly reviewed'):
        relocate.load(config)


def test_named_count_override_does_not_weaken_other_days(layout, tmp_path):
    plan, source, target, _ = layout
    plan['expected_files_per_day'] = 96
    plan['expected_files_by_day'] = {plan['days'][0]: 2}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(plan))
    checked = relocate.load(config)
    assert relocate.expected_files(checked, 'Y2026/M05/D03') == 96
    assert relocate.stage(checked, plan['days'][0])['files'] == 2
    del checked['expected_files_by_day']
    with pytest.raises(RuntimeError, match='file count'):
        relocate.inspect(checked)


@contextmanager
def simulated_bind(plan, source, target):
    """Map logical file reads to the destination; real mount probes run on Linux."""
    hash_file, attributes = relocate.hash_file, relocate.attributes
    mapped = lambda path: target / Path(path).name if Path(path).parent == source else path
    identity = {'logical': str(source), 'destination': str(target),
                'directory_identity': [1, 2, 1, 2]}
    active = Path(plan['state_root']) / 'test-active.json'
    if not active.exists(): relocate.save(active, plan)
    with patch.object(relocate, 'ACTIVE', active), patch.object(relocate, 'guard'), patch.object(relocate, 'mount_identity', return_value=identity), \
            patch.object(relocate, 'hash_file', side_effect=lambda path: hash_file(mapped(path))) as hashes, \
            patch.object(relocate, 'attributes', side_effect=lambda path: attributes(mapped(path))):
        yield hashes


def enter_quarantine(layout):
    plan, source, target, quarantine = layout
    relocate.stage(plan, plan['days'][0])
    quarantine.parent.mkdir(parents=True)
    source.rename(quarantine)
    source.mkdir()
    return plan, source, target, quarantine


def test_staging_receipt_cannot_release_originals(layout):
    plan, source, target, quarantine = enter_quarantine(layout)
    with simulated_bind(plan, source, target), patch.object(relocate, 'assert_paused'), \
            patch.object(relocate, 'inspect'), patch.object(relocate, 'graph'):
        with pytest.raises(RuntimeError, match='current mounted path'):
            relocate.release(plan, plan['days'][0])
    assert len(list(quarantine.iterdir())) == 2
    assert not relocate.progress_path(plan, plan['days'][0]).exists()


def test_mounted_verification_hashes_logical_path_and_quarantine(layout):
    plan, source, target, quarantine = enter_quarantine(layout)
    with simulated_bind(plan, source, target) as hashes:
        evidence = relocate.verify_day(plan, plan['days'][0], mounted=True)
    read = {Path(call.args[0]) for call in hashes.call_args_list if Path(call.args[0]).parent in (source, quarantine)}
    assert read == {root/name for root in (source, quarantine) for name in evidence['files']}
    proof = evidence['mounted_verification']
    assert proof['manifest_sha256'] == relocate.evidence_digest(evidence)
    assert proof['mount_identity']['logical'] == str(source)
    assert proof['cutover_identity']['sha256']


@pytest.mark.parametrize('failure', ['missing mount', 'wrong mounted target', 'changed file identity'])
def test_bad_mount_guard_preserves_previous_proof(layout, failure):
    plan, source, target, _ = enter_quarantine(layout)
    before = relocate.manifest_path(plan, plan['days'][0]).read_bytes()
    with patch.object(relocate, 'guard', side_effect=RuntimeError(failure)), \
            patch.object(relocate, 'hash_file') as hashes:
        with pytest.raises(RuntimeError, match=failure):
            relocate.verify_day(plan, plan['days'][0], mounted=True)
        hashes.assert_not_called()
    assert relocate.manifest_path(plan, plan['days'][0]).read_bytes() == before


def test_mount_changed_after_hashing_cannot_publish_receipt(layout):
    plan, source, target, _ = enter_quarantine(layout)
    before = relocate.manifest_path(plan, plan['days'][0]).read_bytes()
    with simulated_bind(plan, source, target), patch.object(relocate, 'mount_identity', side_effect=[{'directory_identity':[1,2]}, {'directory_identity':[1,3]}]):
        with pytest.raises(RuntimeError, match='Mounted path or active cutover changed'):
            relocate.verify_day(plan, plan['days'][0], mounted=True)
    assert relocate.manifest_path(plan, plan['days'][0]).read_bytes() == before


def test_interrupted_mounted_verification_keeps_previous_receipt(layout):
    plan, source, target, _ = enter_quarantine(layout)
    with simulated_bind(plan, source, target):
        relocate.verify_day(plan, plan['days'][0], mounted=True)
        before = relocate.manifest_path(plan, plan['days'][0]).read_bytes()
        with patch.object(relocate, 'hash_file', side_effect=OSError('interrupted read')):
            with pytest.raises(OSError, match='interrupted read'):
                relocate.verify_day(plan, plan['days'][0], mounted=True)
    assert relocate.manifest_path(plan, plan['days'][0]).read_bytes() == before


def test_rebound_mount_or_changed_manifest_requires_new_mounted_proof(layout):
    plan, source, target, quarantine = enter_quarantine(layout)
    with simulated_bind(plan, source, target):
        proof = relocate.verify_day(plan, plan['days'][0], mounted=True)
        with patch.object(relocate, 'mount_identity', return_value={'directory_identity':[1,3]}):
            with pytest.raises(RuntimeError, match='current mounted path'):
                relocate.require_mounted_verification(plan, plan['days'][0], proof)
        proof['files']['one.LV0']['sha256'] = '0'*64
        with pytest.raises(RuntimeError, match='current mounted path'):
            relocate.require_mounted_verification(plan, plan['days'][0], proof)
    assert len(list(quarantine.iterdir())) == 2


def test_quarantine_verification_for_rollback_does_not_require_working_mount(layout):
    plan, source, target, _ = enter_quarantine(layout)
    with patch.object(relocate, 'guard', side_effect=AssertionError('partial cutover has no usable bind')):
        evidence = relocate.verify_day(plan, plan['days'][0], quarantine_origin=True)
    assert 'mounted_verification' not in evidence


def test_post_read_mount_guard_failure_preserves_prior_proof(layout):
    plan, source, target, _ = enter_quarantine(layout)
    before = relocate.manifest_path(plan, plan['days'][0]).read_bytes()
    with simulated_bind(plan, source, target), patch.object(relocate, 'guard', side_effect=[None, RuntimeError('mount failed after reading')]) as check:
        with pytest.raises(RuntimeError, match='after reading'):
            relocate.verify_day(plan, plan['days'][0], mounted=True)
        assert check.call_count == 2
    assert relocate.manifest_path(plan, plan['days'][0]).read_bytes() == before


def test_new_cutover_with_same_plan_requires_new_mounted_verification(layout):
    plan, source, target, quarantine = enter_quarantine(layout)
    with simulated_bind(plan, source, target):
        proof = relocate.verify_day(plan, plan['days'][0], mounted=True)
        relocate.save(relocate.ACTIVE, plan)  # New atomic record, identical JSON.
        with pytest.raises(RuntimeError, match='current mounted path'):
            relocate.require_mounted_verification(plan, plan['days'][0], proof)
    assert len(list(quarantine.iterdir())) == 2


def test_active_record_changed_during_read_does_not_replace_proof(layout):
    plan, source, target, _ = enter_quarantine(layout)
    before = relocate.manifest_path(plan, plan['days'][0]).read_bytes()
    with simulated_bind(plan, source, target), patch.object(relocate, 'cutover_identity', side_effect=[{'signature':[1]}, {'signature':[2]}]):
        with pytest.raises(RuntimeError, match='active cutover changed'):
            relocate.verify_day(plan, plan['days'][0], mounted=True)
    assert relocate.manifest_path(plan, plan['days'][0]).read_bytes() == before


@pytest.mark.parametrize('enabled, configured_gib, plan_gib, passes', [
    (True, 50, 40, False), (True, 50, 50, True),
    (True, 60, 50, False), (True, 60, 60, True),
    (False, 50, 40, True),
])
def test_prepare_enforces_active_archive_reserve(tmp_path, enabled, configured_gib, plan_gib, passes):
    """Run the actual preparation assertions without installing files or services."""
    tasks = yaml.safe_load((ROOT / 'roles/radar_raw_relocation/tasks/main.yml').read_text())
    plan = yaml.safe_load((ROOT / 'inventory/radar-relocation-20261024.example.yml').read_text())['aurora_radar_relocation_plan']
    plan['data_reserve_bytes'] = plan_gib * relocate.GIB
    play = tmp_path / 'capacity-assertions.yml'
    play.write_text(yaml.safe_dump([{'hosts': 'all', 'gather_facts': False,
        'vars': {'aurora_radar_relocation_plan': plan,
                 'object_store_recovery_enabled': enabled,
                 'object_store_recovery_free_reserve_bytes': configured_gib * relocate.GIB},
        'tasks': tasks[:2]}]))
    result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '--connection', 'local', str(play)],
                            cwd=ROOT, capture_output=True, text=True)
    assert (result.returncode == 0) == passes, result.stdout + result.stderr
