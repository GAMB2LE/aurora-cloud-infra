from contextlib import contextmanager
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'
sys.path[:0] = [str(ROOT), str(ROOT / 'coordinator')]
from delivery_queue import DeliveryQueue
from delivery_supervisor import LaneDeferred, step


def version(name, mtime):
    return {'relative_path': 'Y2026/M11/D13/' + name, 'size': 10,
            'mtime_ns': mtime, 'sha256': 'a' * 64}


def receipt(op):
    return {'operation_id': op['id'], 'lane': op['lane'], 'expected': op['expected'],
            'verified': True, 'evidence': {'gws_readback_verified': True, 'sha256': op['expected']['sha256']},
            'retention_authorized': False, 'eviction_authorized': False}


class Adapter:
    def __init__(self, *, deferred=False, crash=False):
        self.deferred = deferred
        self.crash = crash
        self.launches = []
        self.guarded_object = None

    def observe(self, lane, operation):
        if lane == 'object' and self.deferred:
            raise LaneDeferred('object-store-unavailable')
        return None

    @contextmanager
    def gws_guard(self, object_operation):
        self.guarded_object = object_operation
        yield []

    @contextmanager
    def object_guard(self, gws_operation):
        yield

    def prepare(self, lane, expected):
        return {'worker': lane, 'path': expected['relative_path']}

    def launch(self, lane, operation):
        self.launches.append((lane, operation['id']))
        if self.crash:
            raise RuntimeError('lost launch response')


def test_classified_object_outage_does_not_block_guarded_gws(tmp_path):
    q = DeliveryQueue(tmp_path / 'queue.sqlite')
    first, second = version('old.LV1', 1), version('new.LV1', 2)
    q.add(first, 'backfill'); q.add(second, 'fresh')
    op = q.reserve('gws', first, {'worker': 'prior'}, external_pending=[])
    q.accept(op, receipt(op))
    obj = q.reserve('object', first, {'worker': 'object'})
    adapter = Adapter(deferred=True)
    result = step(q, adapter)
    assert result['object']['state'] == 'deferred'
    assert result['gws']['state'] == 'started'
    assert adapter.guarded_object == obj and q.outstanding('object') == obj
    assert q.outstanding('gws')['expected'] == second
    q.close()


def test_lost_launch_response_is_reconciled_without_second_launch(tmp_path):
    q = DeliveryQueue(tmp_path / 'queue.sqlite')
    item = version('fresh.LV1', 1)
    q.add(item, 'fresh')
    adapter = Adapter(crash=True)
    with pytest.raises(RuntimeError, match='lost launch'):
        step(q, adapter)
    operation = q.outstanding('gws')
    assert operation is not None and len(adapter.launches) == 1
    adapter.crash = False
    assert step(q, adapter)['gws']['state'] == 'awaiting-observation'
    assert q.outstanding('gws') == operation and len(adapter.launches) == 1
    q.close()


def test_unclassified_object_error_stops_new_gws(tmp_path):
    q = DeliveryQueue(tmp_path / 'queue.sqlite')
    old, fresh = version('old.LV1', 1), version('new.LV1', 2)
    q.add(old, 'backfill'); q.add(fresh, 'fresh')
    op = q.reserve('gws', old, {'worker': 'prior'}, external_pending=[])
    q.accept(op, receipt(op))
    q.reserve('object', old, {'worker': 'object'})
    adapter = Adapter()
    def broken(lane, operation): raise ValueError('unclassified evidence')
    adapter.observe = broken
    with pytest.raises(ValueError, match='unclassified'):
        step(q, adapter)
    assert q.outstanding('gws') is None and not adapter.launches
    adapter.observe = lambda lane, operation: (_ for _ in ()).throw(LaneDeferred('gws-lock-busy'))
    with pytest.raises(ValueError, match='not valid for this destination'):
        step(q, adapter)
    assert q.outstanding('gws') is None
    q.close()


def test_lane_backoff_is_durable_and_alerts_after_three_attempts(tmp_path):
    path = tmp_path / 'queue.sqlite'
    q = DeliveryQueue(path)
    item = version('old.LV1', 1)
    q.add(item, 'backfill')
    op = q.reserve('gws', item, {'worker': 'prior'}, external_pending=[], now=100)
    q.accept(op, receipt(op), now=101)
    obj = q.reserve('object', item, {'worker': 'object'}, now=102)
    adapter = Adapter(deferred=True)
    first = step(q, adapter, now=200)['object']['recovery']
    assert first['attempts'] == 1 and first['retry_at'] == 500
    q.close()
    q = DeliveryQueue(path)
    assert step(q, adapter, now=499)['object']['recovery']['attempts'] == 1
    assert step(q, adapter, now=500)['object']['recovery']['attempts'] == 2
    third = step(q, adapter, now=800)['object']['recovery']
    assert third['attempts'] == 3 and third['persistent_alert'] is True
    assert q.outstanding('object') == obj
    q.close()
