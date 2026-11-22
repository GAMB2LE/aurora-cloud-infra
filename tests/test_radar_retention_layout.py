"""Compressed layout acceptance must preserve the entire historical file contract."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat

import pytest

SOURCE = Path(__file__).parents[1] / 'roles/radar_raw_relocation/files/aurora-radar-retention-layout.py'


@pytest.fixture
def layout(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('layout', SOURCE)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.OWNER = os.getuid()
    module.DATA = tmp_path
    module.IMAGES = tmp_path / 'images'
    module.RAW = tmp_path / 'raw'
    module.PROOFS = tmp_path / 'proofs'
    module.READBACKS = tmp_path / 'readbacks'
    module.REGISTRY = tmp_path / 'registry.json'
    if not hasattr(os, 'listxattr'):
        monkeypatch.setattr(os, 'listxattr', lambda p: [], raising=False)
    day, stem = 'Y2026/M05/D02', 'Y2026-M05-D02'
    view = module.RAW / day; view.mkdir(parents=True)
    folder = module.IMAGES / stem; folder.mkdir(parents=True)
    module.PROOFS.mkdir(); module.READBACKS.mkdir()
    expected = {}
    for index in range(96):
        p = view / ('observation-' + str(index))
        p.write_bytes(str(index).encode())
        expected[p.name] = {**module.attributes(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
    proof = module.PROOFS / (stem + '.json')
    proof.write_text(json.dumps({'day': day, 'files': expected}))
    readback = module.READBACKS / (stem + '-source-readback.json')
    readback.write_text('{}')
    image = folder / 'archive.btrfs'; image.write_bytes(b'fixture image'); image.chmod(0o400)
    image_sha = hashlib.sha256(image.read_bytes()).hexdigest()
    proof_sha = hashlib.sha256(proof.read_bytes()).hexdigest()
    receipt = folder / 'verified.json'
    receipt.write_text(json.dumps({'schema': 'retention-radar-local-recovery-image-v1',
        'day': day, 'files': expected, 'image_sha256': image_sha, 'image_bytes': image.stat().st_size,
        'relocation_proof_sha256': proof_sha, 'source_readback_sha256': hashlib.sha256(readback.read_bytes()).hexdigest(),
        'full_content_and_file_metadata_readback': True}))
    route = {'image_sha256': image_sha, 'image_identity': module.signature(image.stat()),
             'receipt_sha256': hashlib.sha256(receipt.read_bytes()).hexdigest(),
             'relocation_manifest_sha256': proof_sha}
    mounted = {'fstype': 'btrfs', 'source': '/dev/loop123', 'vfs-options': 'ro,nodev,nosuid,noexec'}
    monkeypatch.setattr(module, 'mount_info', lambda target: mounted.copy())
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k: str(image) + '\n')
    monkeypatch.setattr(module.os.path, 'ismount', lambda p: True)
    return module, day, route, view, image, receipt, mounted


def test_full_registered_day_contract_is_accepted(layout):
    module, day, route, *_ = layout
    assert module.guard(day, route)['files'] == 96


def test_writable_mount_cannot_replace_science_day(layout):
    module, day, route, _, _, _, mounted = layout
    mounted['vfs-options'] = 'rw,nodev,nosuid,noexec'
    with pytest.raises(ValueError, match='read-only'):
        module.guard(day, route)


def test_one_missing_observation_rejects_whole_day(layout):
    module, day, route, view, *_ = layout
    next(view.iterdir()).unlink()
    with pytest.raises(ValueError, match='coverage'):
        module.guard(day, route)


def test_replaced_image_is_rejected_even_when_same_size(layout):
    module, day, route, _, image, *_ = layout
    image.chmod(0o600); image.write_bytes(b'x' * image.stat().st_size); image.chmod(0o400)
    with pytest.raises(ValueError, match='identity'):
        module.guard(day, route)


def test_receipt_rewrite_is_not_silently_trusted(layout):
    module, day, route, _, _, receipt, _ = layout
    receipt.write_text(receipt.read_text() + '\n')
    with pytest.raises(ValueError, match='proof pins'):
        module.guard(day, route)


def test_wrong_loop_backing_is_rejected(layout, monkeypatch):
    module, day, route, *_ = layout
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k: '/other/image\n')
    with pytest.raises(ValueError, match='different image'):
        module.guard(day, route)


def test_unreviewed_day_cannot_be_registered(layout):
    module, *_ = layout
    module.REGISTRY.write_text(json.dumps({'schema_version': 1, 'routes': {'Y2026/M11/D21': {}}}))
    with pytest.raises(ValueError, match='unreviewed'):
        module.routes()


def test_group_writable_registry_is_rejected(layout):
    module, *_ = layout
    module.REGISTRY.write_text(json.dumps({'schema_version': 1, 'routes': {}}))
    module.REGISTRY.chmod(0o664)
    with pytest.raises(ValueError, match='unsafe control'):
        module.routes()
