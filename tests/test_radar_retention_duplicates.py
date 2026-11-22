"""Only exact archived duplicates may be unlinked; interrupted receipts remain resumable."""
import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / 'roles/radar_raw_relocation/files/radar_retention_evidence.py'


@pytest.fixture
def copies(tmp_path):
    spec = importlib.util.spec_from_file_location('evidence', SOURCE)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    originals, preserved, proc = (tmp_path / n for n in ('originals', 'preserved', 'proc'))
    for path in (originals, preserved, proc): path.mkdir()
    expected = {}
    for index in range(3):
        name = 'science-' + str(index)
        raw = ('observation bytes ' + str(index)).encode()
        (originals / name).write_bytes(raw); (preserved / name).write_bytes(raw)
        expected[name] = module.signature((originals / name).stat())
    def check():
        for index in range(3):
            assert (preserved / ('science-' + str(index))).read_bytes() == ('observation bytes ' + str(index)).encode()
    return module, originals, preserved, proc, expected, tmp_path / 'journal.json', check


def test_only_archived_duplicates_are_removed(copies):
    module, originals, preserved, proc, expected, journal, check = copies
    record = module.remove_duplicates(originals, expected, journal, check, proc=proc)
    assert list(originals.iterdir()) == []
    assert len(list(preserved.iterdir())) == 3
    assert record['complete'] and set(record['deleted']) == set(expected)
    check()


def test_unexpected_observation_blocks_entire_batch(copies):
    module, originals, _, proc, expected, journal, check = copies
    (originals / 'new-science').write_bytes(b'not included in archival proof')
    with pytest.raises(ValueError, match='unexpected files'):
        module.remove_duplicates(originals, expected, journal, check, proc=proc)
    assert len(list(originals.iterdir())) == 4


def test_changed_version_blocks_all_deletions(copies):
    module, originals, _, proc, expected, journal, check = copies
    (originals / 'science-2').write_bytes(b'a new observation version')
    with pytest.raises(ValueError, match='source version changed'):
        module.remove_duplicates(originals, expected, journal, check, proc=proc)
    assert len(list(originals.iterdir())) == 3


def test_failed_live_image_validation_preserves_every_original(copies):
    module, originals, _, proc, expected, journal, _ = copies
    def fail(): raise ValueError('live image unavailable')
    with pytest.raises(ValueError, match='image unavailable'):
        module.remove_duplicates(originals, expected, journal, fail, proc=proc)
    assert len(list(originals.iterdir())) == 3


def test_open_original_is_not_retired(copies):
    module, originals, _, proc, expected, journal, check = copies
    descriptors = proc / '123' / 'fd'; descriptors.mkdir(parents=True)
    (descriptors / '0').symlink_to(originals / 'science-0')
    with pytest.raises(BlockingIOError, match='open descriptor'):
        module.remove_duplicates(originals, expected, journal, check, proc=proc)
    assert len(list(originals.iterdir())) == 3


def test_crash_after_unlink_before_completion_record_is_resumable(copies, monkeypatch):
    module, originals, _, proc, expected, journal, check = copies
    save = module.save
    interrupted = False
    def fail_once(path, record):
        nonlocal interrupted
        if not interrupted and len(record['deleted']) == 1 and record['pending'] is None:
            interrupted = True
            raise OSError('simulated interrupted receipt write')
        return save(path, record)
    monkeypatch.setattr(module, 'save', fail_once)
    with pytest.raises(OSError, match='interrupted receipt'):
        module.remove_duplicates(originals, expected, journal, check, proc=proc)
    assert len(list(originals.iterdir())) == 2
    assert json.loads(journal.read_text())['pending'] == 'science-0'
    record = module.remove_duplicates(originals, expected, journal, check, proc=proc)
    assert record['complete'] and len(set(record['deleted'])) == 3
    assert list(originals.iterdir()) == []
    check()


def test_retired_path_reappearing_is_preserved(copies):
    module, originals, _, proc, expected, journal, check = copies
    module.remove_duplicates(originals, expected, journal, check, proc=proc)
    (originals / 'science-0').write_bytes(b'new science at a previously retired name')
    with pytest.raises(ValueError, match='reappeared'):
        module.remove_duplicates(originals, expected, journal, check, proc=proc)
    assert (originals / 'science-0').read_bytes() == b'new science at a previously retired name'
