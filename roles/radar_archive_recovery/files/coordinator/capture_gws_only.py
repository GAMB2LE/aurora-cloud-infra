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

from bounded_backlog_runner import state, successful
from gws_only_evidence import validate
from gws_worker_release import authenticate_worker
from prepare_direct_candidate import BASE, run_remote

REMOTE = '/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/direct-staged-v1'
FINALIZED = '/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/direct-finalized-gws-v4'
FINALIZED_PINS = {
    'finalized_gws_launcher.py': '436015e61f11eb4c007005a3ebc904fabdb7c7cf9a0531e6ae021c23b0ae18ee',
    'finalized_gws_adapter.py': '6ef7c8ce481f2d6dcd56ef5a95081c3131139466ee8a5d86da8d33bc4eba0fab',
    'finalized_expected.py': 'a11cfc0673b89049f938fa47f7d75af8061df321e498b355ec1f4665d0f63fb1',
    'direct_finalized_recovery.py': 'b70bf8ce666ee510bde61a8cf67cfb16b3524afd5d9a00fb5c1882a02a25377f',
    'lane_recovery_locks.py': 'cf63f02838a82ae299976764f885f073eb3d41306877da955c99f4b41d92f322',
}


def finalized_argv(preparation, request_raw):
    argv = json.loads((preparation / 'launcher-args.json').read_bytes())
    if (not isinstance(argv, list) or len(argv) != 10 or argv[:3] != ['/usr/bin/python3', FINALIZED + '/finalized_gws_launcher.py', '--request']
            or argv[4] != '--request-sha256' or argv[5] != hashlib.sha256(request_raw).hexdigest()
            or argv[6] != '--continuity' or argv[8] != '--continuity-sha256'
            or not isinstance(argv[9], str) or not re.fullmatch('[0-9a-f]{64}', argv[9])):
        raise ValueError('finalized worker command differs from admitted request')
    return argv


def save(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(raw); out.flush(); os.fsync(out.fileno())


def capture(preparation):
    preparation = Path(preparation)
    if preparation.parent != BASE or not re.fullmatch(r'direct-(?:gws-only|finalized)-preparation-20\d{6}T\d{6}Z', preparation.name):
        raise ValueError('invalid preparation path')
    identity = json.loads((preparation / 'service-identity.json').read_bytes())
    unit = identity['unit']
    if not re.fullmatch(r'aurora-direct-20261027-gws-only-(?:20\d{6}t)?\d{6}z-0\.service', unit):
        raise ValueError('invalid worker identity')
    properties = state(unit)
    if not (successful(properties) and properties.get('InvocationID') == identity['invocation_id']
            and properties.get('ExecMainPID') == str(identity['main_pid'])):
        raise ValueError('worker is not the recorded successful instance')
    request_raw = (preparation / 'request.json').read_bytes()
    request = json.loads(request_raw)
    finalized = 'finalized_stage' in request
    if finalized != preparation.name.startswith('direct-finalized-preparation-'):
        raise ValueError('preparation type differs from admitted request')
    pins = {'/etc/aurora/ass-retention.hold': '22dce428efe03586a4cdf751c327a9c34e685922a9907c8b193d5d684cab3e24'}
    if finalized:
        argv = finalized_argv(preparation, request_raw)
        authenticate_worker(unit, argv, run_remote)
        pins.update({FINALIZED + '/' + name: digest for name, digest in FINALIZED_PINS.items()})
    else:
        pins[REMOTE + '/direct_staged_launcher.py'] = '337ae459c847b848a37fcd10b59d8a50bed93a474e56fced577e4e861b43374d'
    for path, wanted in pins.items():
        if run_remote(['sudo', '-n', 'sha256sum', path], timeout=60).stdout.decode().split()[0] != wanted:
            raise ValueError('worker or retention control pin differs')
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
    source = request['expected_source']
    if (expected['relative_path'] != request['relative_path'] or expected['size'] != source['identity'][2]
            or expected['mtime_ns'] != source['identity'][3] or expected['sha256'] != source['sha256']):
        raise ValueError('worker evidence differs from admitted request')
    if finalized and (preflight.get('schema') != 'radar-finalized-stage-preflight-v1'
                      or request['finalized_stage'].get('expected') != expected
                      or preflight.get('handoff', {}).get('stage_operation') != request['finalized_stage'].get('operation')
                      or preflight.get('handoff', {}).get('post_stage_operation') != request['finalized_stage'].get('post_stage_operation')):
        raise ValueError('finalized handoff differs from admitted request')
    result = validate(expected, preflight, raw['journal.jsonl'], raw['commit.jsonl'],
                      json.loads(raw['fence.json']), json.loads(raw['readback.json']))
    output = preparation / 'captured-gws-evidence'
    output.mkdir(mode=0o700)
    for name, value in raw.items():
        save(output / name, value)
    result['service_identity'] = identity
    result['evidence_sha256'] = {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()}
    save(output / 'verification.json', (json.dumps(result, sort_keys=True, indent=2) + '\n').encode())
    fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return result


if __name__ == '__main__':
    print(json.dumps(capture(Path(sys.argv[1]).resolve()), sort_keys=True))
