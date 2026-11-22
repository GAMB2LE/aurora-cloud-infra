"""Reject unsafe evidence while permitting clean raw retention independently of products."""
import copy
import datetime as dt
import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / 'roles/ass_retention/files/retention_window_gate.py'
spec = importlib.util.spec_from_file_location('gate', SOURCE)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
NOW = dt.datetime(2026, 11, 21, 21, tzinfo=dt.timezone.utc)


@pytest.fixture
def proof():
    state = {'raw_retention_ready': True, 'writers_policy': 'independent',
             'clean': False, 'domains': {'raw_retention': {
                 'stable_parity': True, 'complete_verification': True,
                 'failures': [], 'evidence_floor_generated_at': '2026-11-21T18:00:00Z'}}}
    summary = {'generated_at': '2026-11-21T20:50:00Z',
               'streams': {'radar': {key: 0 for key in gate.COUNTERS}}}
    return state, summary


def test_clean_raw_gate_is_independent_of_product_alert(proof):
    state, summary = proof
    gate.require_ready(state, summary, ['radar'], NOW)


@pytest.mark.parametrize('stamp', ['2026-11-21T13:00:00Z', '2026-11-21T22:00:00Z', None,
                                 '2026-11-21T18:00:00'])
def test_stale_future_missing_and_naive_evidence_is_rejected(proof, stamp):
    state, summary = proof
    state['domains']['raw_retention']['evidence_floor_generated_at'] = stamp
    with pytest.raises(ValueError):
        gate.require_ready(state, summary, ['radar'], NOW)


@pytest.mark.parametrize('key,value', [('stable_parity', False), ('complete_verification', False),
                                     ('failures', ['checksum mismatch'])])
def test_incomplete_or_unclean_archive_domain_is_rejected(proof, key, value):
    state, summary = proof
    state['domains']['raw_retention'][key] = value
    with pytest.raises(ValueError):
        gate.require_ready(state, summary, ['radar'], NOW)


@pytest.mark.parametrize('value', [1, False, None, '0'])
def test_counter_must_be_an_actual_integer_zero(proof, value):
    state, summary = proof
    summary['streams']['radar'][gate.COUNTERS[0]] = value
    with pytest.raises(ValueError):
        gate.require_ready(state, summary, ['radar'], NOW)


def test_missing_stream_is_not_assumed_clean(proof):
    state, summary = proof
    with pytest.raises(ValueError):
        gate.require_ready(state, summary, ['missing'], NOW)


def test_signed_receipts_must_cover_each_selected_path_exactly_once():
    rows = [{'path': '/source/one'}, {'path': '/source/two'}]
    receipts = [{'site': 'ass', 'dry_run': False, 'candidate_count': 2,
                 'paths': ['/source/one', '/source/two']}]
    gate.require_receipts(receipts, rows)
    duplicate = copy.deepcopy(receipts)
    duplicate[0]['paths'] = ['/source/one', '/source/one']
    with pytest.raises(ValueError):
        gate.require_receipts(duplicate, rows)
    with pytest.raises(ValueError):
        gate.require_receipts(receipts, rows[:1])


def test_dry_run_is_not_evidence_of_deletion():
    with pytest.raises(ValueError):
        gate.require_receipts([{'site': 'ass', 'dry_run': True, 'candidate_count': 1,
                                'paths': ['/source/one']}], [{'path': '/source/one'}])


def test_source_absence_requires_every_path_and_rejects_refills():
    rows = [{'path': '/source/one'}, {'path': '/source/two'}]
    gate.require_absent({'/source/one': None, '/source/two': None}, rows)
    for observed in ({'/source/one': None},
                     {'/source/one': None, '/source/two': [10, 123]},
                     {'/source/one': None, '/source/two': None, '/unexpected': None}):
        with pytest.raises(ValueError):
            gate.require_absent(observed, rows)
