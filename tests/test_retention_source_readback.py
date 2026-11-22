"""A readback receipt must fail closed on corruption and changing source versions."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat

import pytest


@pytest.fixture
def cohort(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('readback', Path(__file__).parents[1] /
                                                'scripts/verify-relocated-radar-retention.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # macOS control Python may lack Linux xattr APIs; fixtures have no xattrs.
    if not hasattr(os, 'listxattr'):
        monkeypatch.setattr(os, 'listxattr', lambda path: [], raising=False)
    module.ROOT, module.PROOFS, module.OUTPUT = (tmp_path / n for n in ('raw', 'proofs', 'out'))
    module.LOCK = tmp_path / 'relocation.lock'
    module.LOCK.touch()
    day = module.ROOT / 'Y2026/M05/D02'
    day.mkdir(parents=True)
    module.PROOFS.mkdir()
    source = day / 'observation.LV0'
    source.write_bytes(b'original science observation')
    s = source.stat()
    record = {'size': s.st_size, 'mtime_ns': s.st_mtime_ns, 'mode': stat.S_IMODE(s.st_mode),
              'uid': s.st_uid, 'gid': s.st_gid, 'xattrs': {},
              'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    proof = module.PROOFS / 'Y2026-M05-D02.json'
    proof.write_text(json.dumps({'day': 'Y2026/M05/D02', 'files': {source.name: record}}))
    return module, source, proof


def test_complete_readback_preserves_source_and_explicitly_does_not_authorize_deletion(cohort):
    module, source, _ = cohort
    before = source.read_bytes()
    module.main()
    receipt = json.loads(next(module.OUTPUT.glob('*source-readback.json')).read_text())
    assert receipt['files'][source.name]['sha256'] == hashlib.sha256(before).hexdigest()
    assert receipt['deletion_authorized'] is False
    assert source.read_bytes() == before


def test_same_size_same_mtime_corruption_cannot_receive_receipt(cohort):
    module, source, _ = cohort
    s = source.stat()
    source.write_bytes(b'x' * s.st_size)
    os.utime(source, ns=(s.st_atime_ns, s.st_mtime_ns))
    with pytest.raises(ValueError, match='content or source identity'):
        module.main()
    assert not list(module.OUTPUT.glob('*source-readback.json'))


def test_missing_file_cannot_receive_receipt(cohort):
    module, source, _ = cohort
    source.unlink()
    with pytest.raises(ValueError, match='coverage differs'):
        module.main()


def test_replaced_symlink_cannot_be_followed(cohort):
    module, source, _ = cohort
    original = source.with_suffix('.preserved')
    source.rename(original)
    source.symlink_to(original)
    with pytest.raises(ValueError, match='unexpected source file'):
        module.main()
    assert original.read_bytes() == b'original science observation'


def test_changed_proof_during_readback_cannot_receive_receipt(cohort, monkeypatch):
    module, source, proof = cohort
    signature = module.signature
    count = 0
    def racing_signature(path):
        nonlocal count
        count += 1
        if count == 2:
            proof.write_text(proof.read_text() + '\n')
        return signature(path)
    monkeypatch.setattr(module, 'signature', racing_signature)
    with pytest.raises(ValueError, match='receipt changed'):
        module.main()
    assert not list(module.OUTPUT.glob('*source-readback.json'))
