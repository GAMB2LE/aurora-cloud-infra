"""Withdrawal removes only task-created copies, including across interruption."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from test_radar_raw_relocation import layout, relocate

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'roles/radar_raw_relocation/files/aurora-radar-unstage.py'
SPEC = importlib.util.spec_from_file_location('unstage', SCRIPT)
unstage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(unstage)


@pytest.fixture
def staged(layout, monkeypatch):
    plan, source, target, quarantine = layout
    relocate.stage(plan, plan['days'][0])
    config = Path(plan['state_root']) / 'plan.json'
    relocate.save(config, plan)
    helper = Path(relocate.__file__)
    request = {'operation_id': 'radar-stage-withdrawal-test', 'config': str(config), 'helper': str(helper),
               'plan_sha256': unstage.digest(config.read_bytes()),
               'helper_sha256': unstage.digest(helper.read_bytes()),
               'manifests': {day: unstage.digest(relocate.manifest_path(plan, day).read_bytes()) for day in plan['days']}}
    monkeypatch.setattr(unstage, 'OWNER', os.geteuid())
    monkeypatch.setattr(unstage, 'stage_idle', lambda: None)
    monkeypatch.setattr(unstage, 'mounted_paths', lambda: [Path('/')])
    monkeypatch.setattr(unstage, 'closed_users', lambda *_: None)
    monkeypatch.setattr(relocate, 'ACTIVE', config.parent / 'active.json')
    lock = config.parent / 'lock'
    lock.touch()
    monkeypatch.setattr(relocate, 'LOCK', lock)
    return plan, source, target, quarantine, request


def run(staged, **kwargs):
    return unstage.run(relocate, staged[-1], 'a' * 64, **kwargs)


def journal(staged):
    return Path(staged[0]['state_root']) / 'stage-withdrawal.json'


def original(staged):
    source = staged[1]
    return {path.name: (relocate.signature(path.lstat()), path.read_bytes()) for path in source.iterdir()}


def test_dry_run_and_success_preserve_originals_manifests_and_empty_day(staged):
    before = original(staged)
    proof = relocate.manifest_path(staged[0], staged[0]['days'][0]).read_bytes()
    assert run(staged)['status'] == 'validated'
    assert not journal(staged).exists()
    assert len(list(staged[2].iterdir())) == 2
    assert run(staged, apply=True)['status'] == 'complete'
    assert original(staged) == before
    assert staged[2].is_dir() and list(staged[2].iterdir()) == []
    assert relocate.manifest_path(staged[0], staged[0]['days'][0]).read_bytes() == proof
    assert run(staged, apply=True)['status'] == 'already-complete'
    assert original(staged) == before


@pytest.mark.parametrize('side', ['source', 'target'])
@pytest.mark.parametrize('damage', ['missing', 'bytes', 'ctime', 'foreign', 'link'])
def test_changed_or_foreign_files_stop_before_any_unlink(staged, side, damage):
    path = staged[1 if side == 'source' else 2] / 'one.LV0'
    if damage == 'missing': path.unlink()
    elif damage == 'bytes': path.write_bytes(b'changed')
    elif damage == 'ctime': path.chmod(0o600)
    elif damage == 'foreign': path.with_name('foreign').write_bytes(b'keep')
    elif damage == 'link': os.link(path, path.with_name('extra-link'))
    before = original(staged)
    targets = set(staged[2].iterdir())
    with pytest.raises(RuntimeError, match='Original|Target|Unexpected file'):
        run(staged, apply=True)
    assert original(staged) == before
    assert set(staged[2].iterdir()) == targets
    assert not journal(staged).exists()


@pytest.mark.parametrize('state', ['active', 'quarantine', 'release', 'hold', 'mounted'])
def test_cutover_and_release_state_forbid_withdrawal(staged, state):
    plan, source, target, quarantine, request = staged
    before = original(staged)
    if state == 'active': relocate.ACTIVE.write_text('{}')
    elif state == 'quarantine': quarantine.mkdir(parents=True)
    elif state == 'release': relocate.progress_path(plan, plan['days'][0]).write_text('{}')
    elif state == 'hold':
        hold = Path(plan['state_root']) / 'hold'
        hold.touch()
        plan['launch_hold'] = str(hold)
        relocate.save(Path(request['config']), plan)
        request['plan_sha256'] = unstage.digest(Path(request['config']).read_bytes())
    else:
        proof = relocate.manifest_path(plan, plan['days'][0])
        value = json.loads(proof.read_text())
        value['mounted_verification'] = {}
        relocate.save(proof, value)
        request['manifests'][plan['days'][0]] = unstage.digest(proof.read_bytes())
    with pytest.raises(RuntimeError, match='Active|Quarantine|barrier|mounted'):
        run(staged, apply=True)
    assert original(staged) == before
    assert len(list(target.iterdir())) == 2


@pytest.mark.parametrize('mount_kind', ['source', 'target', 'nested', 'ancestor'])
def test_bind_mounts_and_submounts_rejected_even_on_same_device(staged, mount_kind):
    source, target = staged[1:3]
    mount = {'source': source, 'target': target, 'nested': target / 'one.LV0', 'ancestor': target.parent}[mount_kind]
    with patch.object(unstage, 'mounted_paths', return_value=[Path('/'), mount]):
        with pytest.raises(RuntimeError, match='unexpected mount'):
            run(staged, apply=True)
    assert not journal(staged).exists()


@pytest.mark.parametrize('pin', ['plan', 'manifest', 'helper'])
def test_changed_reviewed_pins_refuse(staged, pin):
    request = staged[-1]
    if pin == 'manifest': request['manifests'][staged[0]['days'][0]] = 'b' * 64
    else: request[pin + '_sha256'] = 'b' * 64
    with pytest.raises(RuntimeError, match='Plan|Helper|manifest'):
        run(staged, apply=True)
    assert not journal(staged).exists()


@pytest.mark.parametrize('defect', ['expired', 'future', 'traversal'])
def test_invalid_fresh_proof_refuses_without_deleting(staged, defect):
    plan, _, target, _, request = staged
    path = relocate.manifest_path(plan, plan['days'][0])
    proof = json.loads(path.read_text())
    if defect == 'expired': proof['verified_at'] -= 6 * 3600 + 1
    elif defect == 'future': proof['verified_at'] += 60
    else:
        for key in ('files', 'source_signature', 'destination_signature'):
            proof[key]['../outside'] = proof[key].pop('one.LV0')
    relocate.save(path, proof)
    request['manifests'][plan['days'][0]] = unstage.digest(path.read_bytes())
    with pytest.raises(RuntimeError, match='expired|future|basename'):
        run(staged, apply=True)
    assert len(list(target.iterdir())) == 2


def test_release_ledger_cannot_authorize_missing_original(staged):
    plan = staged[0]
    (staged[1] / 'one.LV0').unlink()
    relocate.save(relocate.progress_path(plan, plan['days'][0]), {'released': ['one.LV0']})
    with pytest.raises(RuntimeError, match='release evidence'):
        run(staged, apply=True)
    assert len(list(staged[2].iterdir())) == 2


@pytest.mark.parametrize('after_unlink', [False, True])
def test_resume_crash_around_unlink_preserves_all_originals(staged, after_unlink):
    before = original(staged)
    checkpoint = unstage.checkpoint
    def crash(path, record, previous):
        if (after_unlink and len(record['completed']) == 1) or (not after_unlink and record['pending']):
            if not after_unlink:
                checkpoint(path, record, previous)  # Durable intent, no unlink yet.
            raise OSError('simulated interruption')
        return checkpoint(path, record, previous)
    with patch.object(unstage, 'checkpoint', side_effect=crash):
        with pytest.raises(OSError, match='interruption'):
            run(staged, apply=True)
    assert original(staged) == before
    assert json.loads(journal(staged).read_text())['pending'] is not None
    assert len(list(staged[2].iterdir())) == (1 if after_unlink else 2)
    with pytest.raises(RuntimeError, match='explicit --resume'):
        run(staged, apply=True)
    assert run(staged, apply=True, resume=True)['status'] == 'complete'
    assert original(staged) == before


def test_missing_resume_journal_refuses(staged):
    with pytest.raises(RuntimeError, match='original withdrawal journal'):
        run(staged, apply=True, resume=True)
    (staged[2] / 'one.LV0').unlink()
    with pytest.raises(RuntimeError, match='missing without intent'):
        run(staged, apply=True)


@pytest.mark.parametrize('damage', ['malformed', 'foreign', 'progress', 'symlink', 'hardlink', 'writable'])
def test_foreign_journal_is_never_adopted(staged, damage):
    before = original(staged)
    if damage == 'malformed': journal(staged).write_text('{')
    elif damage == 'symlink': journal(staged).symlink_to(staged[-1]['config'])
    elif damage == 'hardlink': os.link(staged[-1]['config'], journal(staged))
    else:
        checkpoint = unstage.checkpoint
        def stop(path, record, previous):
            result = checkpoint(path, record, previous)
            if previous is None: raise OSError('stop')
            return result
        with patch.object(unstage, 'checkpoint', side_effect=stop), pytest.raises(OSError):
            run(staged, apply=True)
        value = json.loads(journal(staged).read_text())
        if damage == 'foreign': value['pins']['request_sha256'] = 'b' * 64
        elif damage == 'progress': value['completed'] = [['Y2026/M05/D02', 'foreign']]
        journal(staged).write_text(json.dumps(value))
        if damage == 'writable': journal(staged).chmod(0o666)
    with pytest.raises((RuntimeError, ValueError)):
        run(staged, apply=True, resume=True)
    assert original(staged) == before
    assert len(list(staged[2].iterdir())) == 2


@pytest.mark.parametrize('kind', ['source', 'target', 'directory', 'journal'])
def test_late_change_after_durable_intent_stops_before_unlink(staged, kind):
    checkpoint = unstage.checkpoint
    def change(path, record, previous):
        result = checkpoint(path, record, previous)
        if record['pending']:
            if kind in ('source', 'target'):
                (staged[1 if kind == 'source' else 2] / record['pending'][1]).chmod(0o600)
            elif kind == 'directory':
                staged[2].rename(staged[2].with_name('preserved-target'))
                staged[2].mkdir()
            else:
                # Same bytes, different identity must not be silently adopted.
                data = path.read_bytes()
                path.unlink()
                path.write_bytes(data)
        return result
    with patch.object(unstage, 'checkpoint', side_effect=change):
        with pytest.raises(RuntimeError, match='changed|replaced'):
            run(staged, apply=True)
    assert len(list(staged[1].iterdir())) == 2
    preserved = staged[2].with_name('preserved-target') if kind == 'directory' else staged[2]
    assert len(list(preserved.iterdir())) == 2


def test_partial_and_completed_journal_block_old_mutating_commands(staged):
    journal(staged).write_text('{}')
    for function in (relocate.stage, relocate.verify_day, relocate.cutover):
        args = (staged[0],) if function == relocate.cutover else (staged[0], staged[0]['days'][0])
        with pytest.raises(RuntimeError, match='withdrawal recorded'):
            function(*args)
    journal(staged).unlink()
    journal(staged).symlink_to('/missing')
    with pytest.raises(RuntimeError, match='withdrawal recorded'):
        relocate.stage(staged[0], staged[0]['days'][0])


def test_busy_lock_returns_without_mutation(staged):
    import fcntl
    with relocate.LOCK.open('r') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        with pytest.raises(RuntimeError, match='lock busy'):
            run(staged, apply=True)
    assert not journal(staged).exists()
    assert len(list(staged[2].iterdir())) == 2


def test_unknown_user_or_incomplete_closure_stops(staged):
    for error in (RuntimeError('open user'), PermissionError('incomplete scan')):
        with patch.object(unstage, 'closed_users', side_effect=error):
            with pytest.raises(type(error)):
                run(staged, apply=True)
    assert not journal(staged).exists()


def test_optimized_python_refuses_before_argument_or_helper_work():
    result = subprocess.run([sys.executable, '-O', str(SCRIPT), '--help'], capture_output=True, text=True)
    assert result.returncode != 0 and 'Run without Python optimization' in result.stderr


def test_every_day_is_validated_before_first_unlink(staged):
    plan, source, target, _, request = staged
    second = 'Y2026/M05/D03'
    plan['days'].append(second)
    second_source, second_target, _ = relocate.paths(plan, second)
    second_source.mkdir(parents=True)
    for name in ('one.LV0', 'one.LV1.NC'):
        path = second_source / name
        path.write_bytes(b'second day observation')
        os.utime(path, (1_700_000_000, 1_700_000_000))
    relocate.stage(plan, second)
    relocate.save(Path(request['config']), plan)
    request['plan_sha256'] = unstage.digest(Path(request['config']).read_bytes())
    request['manifests'][second] = unstage.digest(relocate.manifest_path(plan, second).read_bytes())
    (second_source / 'one.LV0').unlink()
    with pytest.raises(RuntimeError, match='Original source changed'):
        run(staged, apply=True)
    assert len(list(target.iterdir())) == len(list(second_target.iterdir())) == 2
    assert not journal(staged).exists()


def test_completed_rerun_needs_no_new_deletion_proof(staged):
    run(staged, apply=True)
    now = unstage.time.time()
    with patch.object(unstage.time, 'time', return_value=now + 86400):
        assert run(staged, apply=True)['status'] == 'already-complete'


def test_incomplete_withdrawal_cannot_resume_after_proof_expires(staged):
    checkpoint = unstage.checkpoint
    def crash(path, record, previous):
        result = checkpoint(path, record, previous)
        if record['pending']: raise OSError('interruption')
        return result
    with patch.object(unstage, 'checkpoint', side_effect=crash), pytest.raises(OSError):
        run(staged, apply=True)
    now = unstage.time.time()
    with patch.object(unstage.time, 'time', return_value=now + 86400):
        with pytest.raises(RuntimeError, match='expired'):
            run(staged, apply=True, resume=True)
    assert len(list(staged[2].iterdir())) == 2
