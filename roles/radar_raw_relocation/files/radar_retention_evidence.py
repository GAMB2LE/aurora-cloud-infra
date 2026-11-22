"""Bound cloud duplicate retirement to exact source versions and dual archive proof."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

ARTIFACTS = Path('/var/lib/aurora-retention-return-20261121')
ORIGINALS = Path('/data/aurora/raw-relocated/rpgfmcw94')
PROOFS = Path('/var/lib/aurora-cloud/radar-relocation-20261024')
IMAGES = Path('/data/aurora/recovery/retention-images-20261121')
DAYS = frozenset('Y2026/M05/D' + d for d in ('02', '03', '04', '05', '24', '26'))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_uid, info.st_gid, info.st_mode, info.st_nlink]


def save(path, record):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.retention-receipt-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def archive_evidence(day, now):
    if day not in DAYS:
        raise ValueError('unreviewed radar retention day')
    stem = day.replace('/', '-')
    source_raw = (ARTIFACTS / (stem + '-source-readback.json')).read_bytes()
    proof_raw = (PROOFS / (stem + '.json')).read_bytes()
    source, proof = json.loads(source_raw), json.loads(proof_raw)
    files = source.get('files', {})
    if (source.get('day') != day or proof.get('day') != day or len(files) != 96
            or set(files) != set(proof.get('files', {}))
            or source.get('proof_sha256') != sha(proof_raw)):
        raise ValueError('source readback does not cover the historical radar day')
    archive_root = ARTIFACTS / (stem + '-archive-readbacks')
    if {p.name for p in archive_root.glob('*.json')} != {n + '.json' for n in files}:
        raise ValueError('complete dual archive readback is missing')
    receipts = {}
    for name, expected in files.items():
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('unsafe observation name')
        if expected['sha256'] != proof['files'][name]['sha256'] or expected['size'] != proof['files'][name]['size']:
            raise ValueError('source readback describes different science bytes')
        raw = (archive_root / (name + '.json')).read_bytes()
        row = json.loads(raw)
        age = now - dt.datetime.fromisoformat(row['verified_at'].replace('Z', '+00:00'))
        if (row.get('schema') != 'retention-radar-dual-archive-readback-v1'
                or row.get('relative_path') != day + '/' + name or row.get('expected') != expected
                or row.get('source_proof_sha256') != sha(source_raw)
                or row.get('gws_sha256_verified') is not True or row.get('object_get_sha256_verified') is not True
                or not dt.timedelta(0) <= age < dt.timedelta(hours=8)):
            raise ValueError('dual archive proof is stale, incomplete or version-mismatched')
        receipts[name] = sha(raw)
    return {'day': day, 'files': files, 'source_proof_sha256': sha(source_raw),
            'historical_proof_sha256': sha(proof_raw), 'archive_receipt_sha256': receipts}


def no_open_originals(expected, *, proc=Path('/proc')):
    identities = {(row[0], row[1]) for row in expected.values()}
    for process in proc.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            descriptors = list((process / 'fd').iterdir())
        except (FileNotFoundError, ProcessLookupError):
            continue
        for descriptor in descriptors:
            try:
                info = descriptor.stat()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if (info.st_dev, info.st_ino) in identities:
                raise BlockingIOError('a superseded original still has an open descriptor')


def remove_duplicates(directory, expected, journal, check_live_image, *, proc=Path('/proc')):
    """Remove only a journaled subset of these exact, already archived versions."""
    if directory.resolve() != directory or os.path.ismount(directory):
        raise ValueError('expected ordinary superseded source directory')
    if not expected:
        raise ValueError('empty retirement candidate set')
    for name, identity in expected.items():
        if Path(name).name != name or name in ('.', '..') or len(identity) != 9:
            raise ValueError('invalid exact-path duplicate identity')
    expected_pin = sha(json.dumps(expected, sort_keys=True).encode())
    before_directory = directory.stat()
    record = json.loads(journal.read_text()) if journal.exists() else {
        'schema': 'radar-verified-duplicate-retirement-v1', 'directory': str(directory),
        'directory_identity': [before_directory.st_dev, before_directory.st_ino],
        'expected_identity_sha256': expected_pin, 'deleted': [], 'pending': None,
        'allocated_bytes_removed': 0}
    if (record.get('schema') != 'radar-verified-duplicate-retirement-v1'
            or record.get('directory') != str(directory)
            or record.get('directory_identity') != [before_directory.st_dev, before_directory.st_ino]
            or record.get('expected_identity_sha256') != expected_pin
            or len(record['deleted']) != len(set(record['deleted']))
            or set(record['deleted']) - set(expected)
            or (record.get('pending') is not None and
                (record['pending'] not in expected or record['pending'] in record['deleted']))):
        raise ValueError('duplicate retirement journal differs')
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before_directory.st_dev, before_directory.st_ino):
            raise ValueError('superseded directory changed before opening')
        present = set(os.listdir(fd))
        allowed_missing = set(record['deleted']) | ({record['pending']} if record['pending'] else set())
        if (present - set(expected) or set(expected) - present - allowed_missing
                or set(record['deleted']) & present):
            raise ValueError('unexpected files, unexplained absence or a retired duplicate reappeared')
        for name in present:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or signature(info) != expected[name]:
                raise ValueError('superseded source version changed: ' + name)
        no_open_originals({n: expected[n] for n in present}, proc=proc)
        check_live_image()
        if record['pending'] and record['pending'] not in present:
            record['deleted'].append(record['pending'])
            record['allocated_bytes_removed'] += record['pending_allocated_bytes']
            record['pending'] = None
        save(journal, record)
        for name in sorted(present):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if signature(info) != expected[name]:
                raise ValueError('superseded version changed immediately before unlink')
            record.update(pending=name, pending_allocated_bytes=info.st_blocks * 512)
            save(journal, record)
            os.unlink(name, dir_fd=fd); os.fsync(fd)
            record['deleted'].append(name)
            record['allocated_bytes_removed'] += record['pending_allocated_bytes']
            record['pending'] = None
            save(journal, record)
        check_live_image()
        record['complete'] = len(record['deleted']) == len(expected)
        record['completed_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        save(journal, record)
        return record
    finally:
        os.close(fd)
