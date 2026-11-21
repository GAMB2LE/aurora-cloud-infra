from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
import finalized_admission as admission
from per_file_queue import Queue


def candidate(name='example.LV1', size=10):
    return {'relative_path': 'Y2026/M11/D13/' + name, 'size': size, 'mtime_ns': 100, 'sha256': 'a'*64}


class Driver:
    def reserve(self, expected, target): return {'unit': 'test.service', 'relative_path': expected['relative_path']}


def test_admission_reserves_before_launch_and_refuses_duplicate(tmp_path, monkeypatch):
    fresh = Queue(tmp_path/'fresh.sqlite'); frozen = Queue(tmp_path/'frozen.sqlite')
    monkeypatch.setattr(admission, 'check', lambda *a, **k: {'object_isolated': True})
    monkeypatch.setattr(admission, 'classify_pending', lambda *a: [])
    expected = candidate()
    op = admission.reserve(fresh, frozen, expected, Driver(), lock_fds={}, base=tmp_path)
    assert fresh.outstanding() == op and op['target'] == 'gws-verified'
    with pytest.raises(ValueError, match='unresolved'):
        admission.reserve(fresh, frozen, expected, Driver(), lock_fds={}, base=tmp_path)
    fresh.close(); frozen.close()


def test_blocks_pairing_and_global_capacity(tmp_path, monkeypatch):
    fresh = Queue(tmp_path/'fresh.sqlite'); frozen = Queue(tmp_path/'frozen.sqlite')
    other = candidate('other.LV1'); frozen.add(other); frozen.begin(other, 'gws-verified', {'unit': 'x'})
    monkeypatch.setattr(admission, 'check', lambda *a, **k: {'object_isolated': True})
    monkeypatch.setattr(admission, 'classify_pending', lambda *a: [])
    with pytest.raises(ValueError, match='historical'):
        admission.reserve(fresh, frozen, candidate(), Driver(), lock_fds={}, base=tmp_path)
    fresh.close(); frozen.close()
    fresh = Queue(tmp_path/'fresh2.sqlite'); frozen = Queue(tmp_path/'frozen2.sqlite')
    monkeypatch.setattr(admission, 'classify_pending', lambda *a: [
        {'expected': candidate(f'large{i}.LV1', 4 * 1024**3), 'status': 'object-copy-needed'} for i in range(4)])
    with pytest.raises(ValueError, match='backlog'):
        admission.reserve(fresh, frozen, candidate(), Driver(), lock_fds={}, base=tmp_path)
    assert fresh.outstanding() is None
    fresh.close(); frozen.close()


@pytest.mark.parametrize('paired_exactly', [True, False])
def test_completed_gws_history_does_not_exhaust_fresh_admission(tmp_path, monkeypatch, paired_exactly):
    fresh = Queue(tmp_path/'history.sqlite'); frozen = Queue(tmp_path/'frozen.sqlite')
    history = [candidate(f'history{i}.LV1', 80 * 1024**2) for i in range(300)]
    for version in history:
        fresh.add(version)
    fresh.db.execute('UPDATE files SET stage=1')
    fresh.db.commit()
    pairs = [dict(version) for version in history]
    if not paired_exactly:
        for version in pairs:
            version['sha256'] = 'b' * 64
    monkeypatch.setattr(admission, 'check', lambda *a, **k: {'object_isolated': True})
    monkeypatch.setattr(admission, 'classify_pending', lambda *a: [
        {'expected': version, 'status': 'provisional-pair-recorded'} for version in pairs])
    if paired_exactly:
        op = admission.reserve(fresh, frozen, candidate('new.LV1'), Driver(), lock_fds={}, base=tmp_path)
        assert fresh.outstanding() == op
    else:
        with pytest.raises(ValueError, match='backlog'):
            admission.reserve(fresh, frozen, candidate('new.LV1'), Driver(), lock_fds={}, base=tmp_path)
        assert fresh.outstanding() is None
    fresh.close(); frozen.close()
