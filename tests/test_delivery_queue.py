import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / 'roles/radar_archive_recovery/files'
sys.path[:0] = [str(ROOT), str(ROOT / 'coordinator')]
from delivery_queue import DeliveryQueue


def version(name, *, size=10, mtime=1):
    return {'relative_path': 'Y2026/M11/D13/' + name, 'size': size,
            'mtime_ns': mtime, 'sha256': 'a' * 64}


def receipt(op):
    evidence = {'gws_readback_verified': True, 'sha256': op['expected']['sha256']}
    if op['lane'] == 'object':
        evidence.update(object_readback_verified=True, pair_recorded=True,
                        ledger_replicated=True, snapshot_sha256='b' * 64)
    return {'operation_id': op['id'], 'lane': op['lane'], 'expected': op['expected'],
            'verified': True, 'evidence': evidence, 'retention_authorized': False,
            'eviction_authorized': False}


def test_unresolved_object_does_not_block_other_gws_file_and_restart(tmp_path):
    path = tmp_path / 'queue.sqlite'
    q = DeliveryQueue(path)
    first, second = version('a.LV1'), version('b.LV1', mtime=2)
    q.add(first, 'fresh')
    q.add(second, 'fresh')
    gws = q.reserve('gws', first, {'unit': 'gws1'}, external_pending=[])
    q.accept(gws, receipt(gws))
    obj = q.reserve('object', first, {'unit': 'object1'})
    gws2 = q.reserve('gws', second, {'unit': 'gws2'}, external_pending=[first])
    assert q.outstanding('object') == obj
    with pytest.raises(ValueError, match='already has an operation'):
        q.reserve('gws', second, {'unit': 'duplicate'}, external_pending=[first])
    q.close()
    q = DeliveryQueue(path)
    assert q.outstanding('object') == obj and q.outstanding('gws') == gws2
    q.accept(gws2, receipt(gws2))
    assert q.status()['gws_only_files'] == 2
    q.accept(obj, receipt(obj))
    assert q.status()['gws_only_files'] == 1
    q.close()


def test_global_capacity_and_exact_receipts_fail_closed(tmp_path):
    q = DeliveryQueue(tmp_path / 'queue.sqlite')
    item = version('new.LV1')
    q.add(item, 'fresh')
    with pytest.raises(ValueError, match='inventory'):
        q.reserve('gws', item, {'unit': 'x'})
    other = [version(f'old{i}.LV1') for i in range(256)]
    with pytest.raises(ValueError, match='capacity'):
        q.reserve('gws', item, {'unit': 'x'}, external_pending=other)
    op = q.reserve('gws', item, {'unit': 'x'}, external_pending=[])
    invalid = receipt(op)
    invalid['evidence']['sha256'] = 'f' * 64
    with pytest.raises(ValueError, match='GWS version proof'):
        q.accept(op, invalid)
    assert q.outstanding('gws') == op
    q.accept(op, receipt(op))
    object_op = q.reserve('object', item, {'unit': 'y'})
    invalid = receipt(object_op)
    invalid['evidence']['ledger_replicated'] = False
    with pytest.raises(ValueError, match='replicated checkpoint'):
        q.accept(object_op, invalid)
    assert q.outstanding('object') == object_op
    q.close()


def test_fresh_priority_does_not_starve_backfill(tmp_path):
    q = DeliveryQueue(tmp_path / 'queue.sqlite')
    for i in range(4):
        q.add(version(f'fresh{i}.LV1', mtime=10+i), 'fresh')
    old = version('old.LV0', mtime=1)
    q.add(old, 'backfill')
    chosen = []
    for _ in range(4):
        item = q.next_gws(external_pending=[])
        chosen.append(item)
        op = q.reserve('gws', item, {'unit': 'gws'}, external_pending=[])
        q.accept(op, receipt(op))
    assert all(item != old for item in chosen[:3])
    assert chosen[3] == old
    q.close()


def test_scheduler_skips_candidate_that_cannot_fit_remaining_capacity(tmp_path):
    q = DeliveryQueue(tmp_path / 'queue.sqlite')
    large = version('large.LV1', size=4 * 1024**3, mtime=20)
    small = version('small.LV1', size=1, mtime=10)
    q.add(large, 'fresh')
    q.add(small, 'fresh')
    external = [version(f'old{i}.LV0', size=3 * 1024**3) for i in range(5)]
    assert q.next_gws(external_pending=external) == small
    q.close()


def test_object_lane_also_balances_fresh_and_backfill(tmp_path):
    q = DeliveryQueue(tmp_path / 'queue.sqlite')
    fresh = [version(f'fresh{i}.LV1', mtime=10+i) for i in range(4)]
    old = version('old.LV0', mtime=1)
    for item in fresh + [old]:
        q.add(item, 'backfill' if item == old else 'fresh')
        op = q.reserve('gws', item, {'unit': 'gws'}, external_pending=[])
        q.accept(op, receipt(op))
    chosen = []
    for _ in range(4):
        item = q.next_object()
        chosen.append(item)
        op = q.reserve('object', item, {'unit': 'object'})
        q.accept(op, receipt(op))
    assert all(item != old for item in chosen[:3])
    assert chosen[3] == old
    q.close()
