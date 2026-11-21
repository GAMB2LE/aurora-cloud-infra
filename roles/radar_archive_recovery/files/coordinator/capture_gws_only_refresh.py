#!/usr/bin/env python3
"""Capture a terminal GWS-only worker's evidence; never authorize eviction.

This does not update the archive ledger or establish object-store replication.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from bounded_backlog_runner import state, successful
from gws_only_evidence import validate
from prepare_direct_candidate import BASE, run_remote
from gws_worker_release import resolve_preparation, authenticate_worker, verify_release_pins

REMOTE = '/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/direct-staged-v1'


def validate_refreshed(raw, digest):
    if not raw or len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('refreshed continuity receipt changed')
    proof = json.loads(raw)
    if not (proof.get('accepted') is True and proof.get('files') == 640
            and proof.get('plan_sha256') == 'e9534ca975fd24517bc0db56fcb9a5bb268c2e16b5d07ce29a5b9fa6621d8bcb'
            and proof.get('release_acceptance_sha256') == 'f0edc78e2d99775dff9eff914f8eadd053e98b57f4e8346b8add8953d651f238'
            and proof.get('fresh_payload_hash') is False and proof.get('release_authorized') is False):
        raise ValueError('refreshed continuity evidence differs')
    return proof


def save(path, raw):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.capture-') as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
        try:
            os.link(out.name, path, follow_symlinks=False)
        except FileExistsError:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as existing:
                if existing.read(len(raw) + 1) != raw:
                    raise ValueError(f'existing captured evidence differs: {path.name}')
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def capture(preparation):
    preparation = Path(preparation)
    if preparation.parent != BASE or not re.fullmatch(r'direct-gws-only-preparation-20\d{6}T\d{6}Z', preparation.name):
        raise ValueError('invalid preparation path')
    identity = json.loads((preparation / 'service-identity.json').read_bytes())
    unit = identity['unit']
    if not re.fullmatch(r'aurora-direct-20261027-gws-only-(?:20\d{6}t)?\d{6}z-0\.service', unit):
        raise ValueError('invalid worker identity')
    properties = state(unit)
    if not (successful(properties) and properties.get('InvocationID') == identity['invocation_id']
            and properties.get('ExecMainPID') == str(identity['main_pid'])):
        raise ValueError('worker is not the recorded successful instance')
    release = resolve_preparation(preparation, authenticate=lambda unit, argv:
        authenticate_worker(unit, argv, run_remote))
    verify_release_pins(release, run_remote)
    retention = run_remote(['sudo','-n','sha256sum','/etc/aurora/ass-retention.hold'],timeout=60)
    if retention.stdout.decode().split()[:1] != ['22dce428efe03586a4cdf751c327a9c34e685922a9907c8b193d5d684cab3e24']:
        raise ValueError('retention control pin differs')
    fence = run_remote(['sudo', '-n', 'python3', '-c',
                        'import os; print(int(os.path.lexists("/etc/aurora/gws-direct-publication.fence")))'], timeout=60)
    if fence.stdout.strip() != b'0':
        raise ValueError('GWS publication fence remains')
    operation = unit.removeprefix('aurora-direct-').removesuffix('.service')
    stem = REMOTE + '/transfer-' + operation + '.jsonl'
    files = {'preflight.json': stem + '.preflight.json', 'journal.jsonl': stem,
             'commit.jsonl': stem + '.commit.jsonl', 'fence.json': stem + '.fence-reconciliation.json',
             'readback.json': stem + '.readback.json'}
    raw = {}
    for name, path in files.items():
        value = run_remote(['sudo', '-n', 'cat', path], timeout=60).stdout
        if not value or len(value) > 1024 * 1024:
            raise ValueError('empty or oversized remote evidence')
        digest = run_remote(['sudo', '-n', 'sha256sum', path], timeout=60).stdout.decode().split()[0]
        if digest != hashlib.sha256(value).hexdigest():
            raise ValueError('remote evidence changed during capture')
        raw[name] = value
    preflight = json.loads(raw['preflight.json'])
    expected = preflight['expected']
    request = json.loads((preparation / 'request.json').read_bytes())
    source = request['expected_source']
    if (expected['relative_path'] != request['relative_path'] or expected['size'] != source['identity'][2]
            or expected['mtime_ns'] != source['identity'][3] or expected['sha256'] != source['sha256']):
        raise ValueError('worker evidence differs from admitted request')
    result = validate(expected, preflight, raw['journal.jsonl'], raw['commit.jsonl'],
                      json.loads(raw['fence.json']), json.loads(raw['readback.json']))
    refreshed = run_remote(['sudo', '-n', 'cat', stem + '.continuity-refresh.json'], timeout=60).stdout
    digest = run_remote(['sudo', '-n', 'sha256sum', stem + '.continuity-refresh.json'], timeout=60).stdout.decode().split()[0]
    validate_refreshed(refreshed, digest)
    output = preparation / 'captured-gws-evidence'
    output.mkdir(mode=0o700, exist_ok=True)
    if output.is_symlink() or not output.is_dir():
        raise ValueError('invalid captured evidence directory')
    fd = os.open(preparation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    for name, value in raw.items():
        save(output / name, value)
    save(output / 'continuity-refresh.json', refreshed)
    result['continuity_refresh_sha256'] = digest
    result['service_identity'] = identity
    result['evidence_sha256'] = {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()}
    save(output / 'verification.json', (json.dumps(result, sort_keys=True, indent=2) + '\n').encode())
    fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return result


if __name__ == '__main__':
    print(json.dumps(capture(Path(sys.argv[1]).resolve()), sort_keys=True))
