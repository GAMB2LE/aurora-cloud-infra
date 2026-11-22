"""A replacement cannot hide a legacy day from the historical cohort checks."""
from pathlib import Path
import sys
import types

from jinja2 import Template
import pytest


@pytest.fixture
def mixed(monkeypatch):
    helper = Path('/pinned-helper')
    day13 = 'Y2026/M05/D13'
    plan_sha = 'e9534ca975fd24517bc0db56fcb9a5bb268c2e16b5d07ce29a5b9fa6621d8bcb'
    fake = types.ModuleType('verify_relocated_may_d13_mount')
    fake.stage = types.SimpleNamespace(DAY=day13, HELPER=helper, HELPER_SHA='helper-pin',
        digest=lambda p: 'helper-pin' if p == helper else plan_sha)
    fake.ROUTE = Path('/not-present')
    fake.guard = lambda route: {'verified_day': day13, 'files': 64}
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    source = Path(__file__).parents[1] / 'roles/radar_raw_relocation/templates/verify_radar_mount_layout.py.j2'
    namespace = {'__name__': 'mixed_layout'}
    exec(Template(source.read_text()).render(retention_layout_sha256='a' * 64), namespace)
    days = ['Y2026/M05/D' + d for d in ('02', '03', '04', '05', '13', '24', '26')]
    plan = {'days': days, 'expected_files_by_day': {day13: 64}}
    checked = []
    monkeypatch.setattr(namespace['runpy'], 'run_path', lambda p: {
        'load': lambda p: plan, 'guard': lambda p, additional_bytes: checked.append((p, additional_bytes))})
    return namespace, checked


def test_two_verified_replacements_leave_four_legacy_days_checked(mixed):
    module, checked = mixed
    selected = {'Y2026/M05/D02': {}, 'Y2026/M05/D03': {}}
    called = []
    module['retention_module'] = lambda: types.SimpleNamespace(
        routes=lambda: (selected, 'registry-pin'),
        guard=lambda day, route: called.append(day) or {'files': 96})
    result = module['verify_layout'](123, route={'old': 'verified'})
    assert result['legacy_bind_days'] == 4
    assert set(result['retention_compressed_routes']) == set(selected)
    assert set(called) == set(selected)
    assert checked[0][1] == 123
    assert set(checked[0][0]['days']) == {'Y2026/M05/D04', 'Y2026/M05/D05', 'Y2026/M05/D24', 'Y2026/M05/D26'}


def test_registry_outside_original_cohort_cannot_reduce_legacy_checks(mixed):
    module, checked = mixed
    module['retention_module'] = lambda: types.SimpleNamespace(
        routes=lambda: ({'Y2026/M11/D21': {}}, 'pin'))
    with pytest.raises(ValueError, match='escape'):
        module['verify_layout'](route={})
    assert checked == []


def test_failed_replacement_validation_cannot_be_omitted_from_legacy_check(mixed):
    module, checked = mixed
    def fail(day, route):
        raise ValueError('image mismatches historical observations')
    module['retention_module'] = lambda: types.SimpleNamespace(
        routes=lambda: ({'Y2026/M05/D02': {}}, 'pin'), guard=fail)
    with pytest.raises(ValueError, match='mismatches'):
        module['verify_layout'](route={})
    assert checked == []


def test_registry_change_during_check_invalidates_result(mixed):
    module, _ = mixed
    observations = iter([({}, 'first'), ({}, 'second')])
    module['retention_module'] = lambda: types.SimpleNamespace(routes=lambda: next(observations))
    with pytest.raises(ValueError, match='controls changed'):
        module['verify_layout'](route={})
