#!/usr/bin/env python3
"""Validate explicitly registered, byte-verified compressed radar day replacements."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

REGISTRY = Path('/etc/aurora/radar-retention-compressed.json')
RAW = Path('/project/aurora/raw/rpgfmcw94')
IMAGES = Path('/data/aurora/recovery/retention-images-20261121')
PROOFS = Path('/var/lib/aurora-cloud/radar-relocation-20261024')
READBACKS = Path('/var/lib/aurora-retention-return-20261121')
DAYS = frozenset('Y2026/M05/D' + day for day in ('02', '03', '04', '05', '24', '26'))
OWNER = 0
DATA = Path('/data')


def signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_uid, info.st_gid, info.st_mode, info.st_nlink]


def control(path):
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('unsafe control path')
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != OWNER or before.st_mode & 0o022
            or before.st_nlink != 1 or before.st_size > 2 * 1024**2):
        raise ValueError('unsafe control file: ' + str(path))
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        if signature(os.fstat(stream.fileno())) != signature(before):
            raise ValueError('control replaced before reading')
        raw = stream.read(2 * 1024**2 + 1)
        after = os.fstat(stream.fileno())
    if signature(before) != signature(after) or signature(path.lstat()) != signature(before):
        raise ValueError('control changed while reading')
    return raw


def routes():
    if not os.path.lexists(REGISTRY):
        return {}, None
    raw = control(REGISTRY)
    value = json.loads(raw)
    if set(value) != {'schema_version', 'routes'} or value['schema_version'] != 1:
        raise ValueError('unknown compressed retention registry contract')
    result = value['routes']
    if not isinstance(result, dict) or set(result) - DAYS:
        raise ValueError('unreviewed compressed radar day')
    return result, hashlib.sha256(raw).hexdigest()


def attributes(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('unexpected mounted radar file type')
    return {'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
            'xattrs': {key: os.getxattr(path, key).hex() for key in os.listxattr(path)}}


def mount_info(target):
    raw = subprocess.check_output(['findmnt', '--json', '--mountpoint', str(target),
                                   '-o', 'FSTYPE,SOURCE,VFS-OPTIONS'], text=True, timeout=30)
    return json.loads(raw)['filesystems'][0]


def validate_files(target, expected):
    if len(expected) != 96 or set(p.name for p in target.iterdir()) != set(expected):
        raise ValueError('compressed radar day coverage differs')
    for name, row in expected.items():
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('unsafe radar proof filename')
        if attributes(target / name) != {k: v for k, v in row.items() if k != 'sha256'}:
            raise ValueError('compressed radar metadata differs: ' + name)


def guard(day, route, *, target=None):
    if day not in DAYS:
        raise ValueError('unreviewed radar replacement day')
    fields = {'image_sha256', 'image_identity', 'receipt_sha256', 'relocation_manifest_sha256'}
    if set(route) != fields or any(not re.fullmatch('[a-f0-9]{64}', route[k])
                                  for k in fields - {'image_identity'}):
        raise ValueError('invalid compressed radar route')
    target = target if target is not None else RAW / day
    folder = IMAGES / day.replace('/', '-')
    image, receipt_path = folder / 'archive.btrfs', folder / 'verified.json'
    if image.resolve() != image or any(p.is_symlink() for p in image.parents):
        raise ValueError('unsafe compressed image path')
    before = image.lstat()
    if (not os.path.ismount(DATA) or before.st_dev != DATA.stat().st_dev
            or not stat.S_ISREG(before.st_mode) or before.st_uid != OWNER or before.st_nlink != 1
            or before.st_mode & 0o022 or signature(before) != route['image_identity']):
        raise ValueError('compressed image identity differs')
    receipt_raw = control(receipt_path)
    proof_raw = control(PROOFS / (day.replace('/', '-') + '.json'))
    readback_raw = control(READBACKS / (day.replace('/', '-') + '-source-readback.json'))
    if (hashlib.sha256(receipt_raw).hexdigest() != route['receipt_sha256']
            or hashlib.sha256(proof_raw).hexdigest() != route['relocation_manifest_sha256']):
        raise ValueError('compressed route proof pins differ')
    receipt, proof = json.loads(receipt_raw), json.loads(proof_raw)
    if (receipt.get('schema') != 'retention-radar-local-recovery-image-v1'
            or receipt.get('day') != day or proof.get('day') != day
            or receipt.get('image_sha256') != route['image_sha256']
            or receipt.get('image_bytes') != before.st_size
            or receipt.get('relocation_proof_sha256') != route['relocation_manifest_sha256']
            or receipt.get('source_readback_sha256') != hashlib.sha256(readback_raw).hexdigest()
            or receipt.get('files') != proof.get('files')
            or receipt.get('full_content_and_file_metadata_readback') is not True):
        raise ValueError('compressed image readback does not describe historical science files')
    mounted = mount_info(target)
    if mounted['fstype'] != 'btrfs' or not {'ro', 'nodev', 'nosuid', 'noexec'} <= set(mounted['vfs-options'].split(',')):
        raise ValueError('compressed day must be a read-only restricted Btrfs mount')
    device = mounted['source'].split('[', 1)[0]
    if not re.fullmatch('/dev/loop[0-9]+', device):
        raise ValueError('unexpected compressed day backing device')
    backing = subprocess.check_output(['losetup', '--noheadings', '--output', 'BACK-FILE', device],
                                     text=True, timeout=30).strip()
    if backing != str(image):
        raise ValueError('compressed day uses a different image')
    validate_files(target, proof['files'])
    if signature(image.lstat()) != signature(before) or mount_info(target) != mounted:
        raise ValueError('compressed image or mount changed during validation')
    if control(receipt_path) != receipt_raw:
        raise ValueError('compressed image receipt changed during validation')
    return {'verified_day': day, 'files': 96, 'image_sha256': route['image_sha256']}


def verify_routes():
    selected, pin = routes()
    result = {day: guard(day, route) for day, route in sorted(selected.items())}
    if routes() != (selected, pin):
        raise ValueError('compressed retention registry changed during validation')
    return result
