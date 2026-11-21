"""A finished coordinator is compatible only when its frozen work is complete."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
import audit_frozen_cohort
import completed_coordinator_gate as gate
import finalized_fresh_controller as controller
import per_file_coordinator
import per_file_queue


@pytest.fixture
def completed(monkeypatch, tmp_path):
    candidates = [dict(relative_path=f'file-{i}', size=i + 1, mtime=100) for i in range(566)]
    fixture = dict(count=750, unfinished=0, outstanding=None, candidates=candidates,
                   identities=[json.dumps(dict(relative_path=r['relative_path'], size=r['size'],
                                               mtime_ns=r['mtime'] * 10**9)) for r in candidates],
                   obligations={str(i): {} for i in range(581)},
                   ledger=[dict(state='pair-recorded') for _ in range(581)])

    class Database:
        def execute(self, sql):
            if 'count(*)' in sql:
                return SimpleNamespace(fetchone=lambda: (fixture['count'], fixture['unfinished']))
            return [(row,) for row in fixture['identities']]

    class Queue:
        def __init__(self, path, *, readonly):
            assert readonly is True
            self.db = Database()
        def outstanding(self): return fixture['outstanding']
        def close(self): pass

    monkeypatch.setattr(per_file_coordinator, 'BASE', tmp_path)
    monkeypatch.setattr(per_file_coordinator, 'load_candidates', lambda *args: fixture['candidates'])
    monkeypatch.setattr(per_file_queue, 'Queue', Queue)
    monkeypatch.setattr(audit_frozen_cohort, 'obligations', lambda *args: (fixture['obligations'], {}))
    monkeypatch.setattr(audit_frozen_cohort, 'ledger_report', lambda *args: fixture['ledger'])
    props = dict(ActiveState='inactive', SubState='dead', MainPID='0', Result='success', ExecMainStatus='0',
                 ExecStart=f'argv[]=/usr/bin/python3 {controller.R7} run --manifest frozen')
    fixture['props'] = props
    monkeypatch.setattr(gate.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(stdout='\n'.join(f'{k}={v}' for k, v in props.items())))
    return fixture


def test_verified_historical_completion_accepts_an_inactive_service(completed):
    gate.require_completed()


@pytest.mark.parametrize('field,value', [
    ('Result', 'failed'), ('ExecMainStatus', '1'), ('MainPID', '12'),
    ('ExecStart', 'argv[]=/usr/bin/python3 /other/coordinator.py run '),
])
def test_wrong_exit_or_release_is_not_completion(completed, field, value):
    completed['props'][field] = value
    with pytest.raises(ValueError, match='completion is unproven'):
        gate.require_completed()


@pytest.mark.parametrize('change', ['too_few', 'unfinished', 'intent', 'missing_version',
                                    'missing_candidate', 'missing_obligation', 'missing_pair'])
def test_incomplete_frozen_work_never_credits_completion(completed, change):
    if change == 'too_few': completed['count'] = 749
    if change == 'unfinished': completed['unfinished'] = 1
    if change == 'intent': completed['outstanding'] = {'id': 'unresolved'}
    if change == 'missing_version': completed['identities'].pop()
    if change == 'missing_candidate': completed['candidates'].pop()
    if change == 'missing_obligation': completed['obligations'].pop('0')
    if change == 'missing_pair': completed['ledger'][0]['state'] = 'missing-pair'
    with pytest.raises(ValueError):
        gate.require_completed()


def test_fresh_controller_uses_the_completed_gate(completed):
    controller.require_r7()


def test_failed_service_cannot_fall_back_to_completed_gate(completed):
    completed['props']['ActiveState'] = 'failed'
    with pytest.raises(ValueError, match='not running'):
        controller.require_r7()
