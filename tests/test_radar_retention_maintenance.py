"""Normal admission checks must not resume timers before protection is proved."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import types

import pytest

SOURCE = Path(__file__).parents[1] / 'roles/radar_raw_relocation/files/radar_retention_maintenance.py'


@pytest.fixture
def window(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('maintenance', SOURCE)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.HOLD = tmp_path / 'owned-barrier'
    calls = []
    utility = types.SimpleNamespace(control=lambda p: p.read_bytes(), syncdir=lambda p: None,
        save=lambda p, d: p.write_text(json.dumps(d)), run=lambda args, **kw: calls.append(args), FIELD=['field'])
    instance = module.Maintenance('test-window', folder=tmp_path, utility=utility)
    instance.state = {'safe_to_resume': False, 'barrier_created': True, 'barrier_content': 'owned\n',
                      'resume_cloud_units': ['cloud.timer'], 'resume_field_units': ['field.timer']}
    module.HOLD.write_text('owned\n')
    lock = tmp_path / 'relocation.lock'; lock.touch()
    fd = os.open(lock, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    instance.fds = [fd]; instance.relocation_fd = fd
    yield module, instance, lock, calls
    instance.close_locks()


def test_normal_probe_runs_without_barrier_and_under_shared_lease(window):
    module, instance, lock, calls = window
    def check():
        assert not module.HOLD.exists()
        with lock.open() as other:
            fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return {'verified': 640}
    assert instance.handoff_check(check) == {'verified': 640}
    assert calls == []
    instance.safe(); instance.restore()
    assert calls[0] == ['systemctl', 'start', 'cloud.timer']


def test_failed_probe_reinstates_barrier_and_exclusive_protection(window):
    module, instance, lock, calls = window
    def failed():
        raise ValueError('cohort incomplete')
    with pytest.raises(ValueError, match='incomplete'):
        instance.handoff_check(failed)
    assert module.HOLD.read_text() == 'owned\n'
    with lock.open() as other:
        with pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
    assert calls == []


def test_unverified_mutation_does_not_resume_admissions(window):
    module, instance, _, calls = window
    with pytest.raises(RuntimeError, match='unverified'):
        instance.restore()
    assert module.HOLD.read_text() == 'owned\n'
    assert calls == []


def test_changed_operator_barrier_is_not_overwritten(window):
    module, instance, _, calls = window
    module.HOLD.write_text('new operator decision')
    with pytest.raises(ValueError, match='another operator'):
        instance.reenter_exclusive()
    assert module.HOLD.read_text() == 'new operator decision'
    assert calls == []


def test_all_reviewed_admission_timers_are_journaled_before_drain(window):
    module, instance, _, calls = window
    plan={'consumers':['raw.service','mirror.service'],'launchers':['coordinator.service']}
    instance.t.digest=lambda p:module.PLAN_SHA
    instance.t.control=lambda p:json.dumps(plan).encode()
    instance.t.props=lambda unit,field=False:{'ActiveState':'active'}
    def command(args,**kw):
        calls.append(args)
        return 'loaded\n' if args[:2]==['systemctl','show'] else ''
    instance.t.run=command
    assert instance.pause_plan_timers()==plan
    stops=[a[2] for a in calls if a[:2]==['systemctl','stop']]
    assert stops==['raw.timer','mirror.timer','coordinator.timer']
    assert set(stops)<=set(instance.state['resume_cloud_units'])


def test_unreviewed_plan_cannot_pause_other_services(window):
    module, instance, _, calls=window
    instance.t.digest=lambda p:'changed'
    with pytest.raises(ValueError,match='plan differs'):
        instance.pause_plan_timers()
    assert calls==[]


def test_admission_timeout_is_retryable_only_after_restoration(window):
    module, instance, lock, calls=window
    instance.state['safe_to_resume']=True
    with pytest.raises(module.AdmissionDeferred,match='payload still running'):
        instance.fail_admission(TimeoutError('payload still running'))
    assert instance.state['restored_at']
    assert not module.HOLD.exists()
    assert calls==[['systemctl','start','cloud.timer'],['field','systemctl start field.timer']]
    with lock.open() as other:
        fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)


def test_unsafe_state_cannot_become_retryable_admission_deferral(window):
    module, instance, _, calls=window
    with pytest.raises(RuntimeError,match='unverified'):
        instance.fail_admission(TimeoutError('still waiting'))
    assert module.HOLD.exists() and not calls
    assert not instance.state.get('restored_at')


def test_unexpected_admission_error_restores_but_stops_for_investigation(window):
    module, instance, _, calls=window
    instance.state['safe_to_resume']=True
    with pytest.raises(ValueError,match='unexpected control change'):
        instance.fail_admission(ValueError('unexpected control change'))
    assert instance.state['restored_at'] and not module.HOLD.exists()
