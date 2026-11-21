#!/usr/bin/env python3
"""Capture the exact recovered worker's GWS evidence; never release data."""
import hashlib
import json
import os
from pathlib import Path

from bounded_backlog_runner import state, successful
from capture_gws_only import BASE, REMOTE, save
from gws_only_evidence import validate
from prepare_direct_candidate import run_remote

PREPARATION = BASE / 'direct-gws-only-preparation-20261107T103414Z'
UNIT = 'aurora-direct-20261027-gws-resume-103414z-0.service'
STEM = REMOTE + '/transfer-20261027-gws-only-103414z-0.jsonl'
PREFLIGHT_SHA = '8b9b1d08a732f14095589b49c8528c1af15e9bfa24293eaeb34ae7f611165092'
JOURNAL_SHA = 'caee1fc22e844d0a54c0e2dd9cf752b79376b335aff6db6f414cb09c71d71e1f'
HOLD_SHA = '22dce428efe03586a4cdf751c327a9c34e685922a9907c8b193d5d684cab3e24'


def capture(identity, launcher_sha):
    if identity.get('unit') != UNIT or not isinstance(identity.get('main_pid'), int):
        raise ValueError('wrong recovery identity')
    props = state(UNIT)
    if not (successful(props) and props.get('InvocationID') == identity.get('invocation_id')
            and props.get('ExecMainPID') == str(identity['main_pid'])):
        raise ValueError('recovery worker is not the recorded successful instance')
    pins = {REMOTE + '/resume_stage_launcher_103414.py': launcher_sha, '/etc/aurora/ass-retention.hold': HOLD_SHA}
    for path, wanted in pins.items():
        actual = run_remote(['sudo', '-n', 'sha256sum', path], timeout=60).stdout.decode().split()[0]
        if actual != wanted:
            raise ValueError('recovery launcher or retention pin differs')
    marker = run_remote(['sudo', '-n', 'python3', '-c',
                         'import os;print(int(os.path.lexists("/etc/aurora/gws-direct-publication.fence")))'])
    if marker.stdout.strip() != b'0':
        raise ValueError('GWS publication fence remains')
    paths = {'preflight.json': STEM + '.preflight.json', 'journal.jsonl': STEM,
             'commit.jsonl': STEM + '.commit.jsonl', 'fence.json': STEM + '.fence-reconciliation.json',
             'readback.json': STEM + '.readback.json'}
    raw = {}
    for name, path in paths.items():
        value = run_remote(['sudo', '-n', 'cat', path], timeout=60).stdout
        if not value or len(value) > 1024 * 1024:
            raise ValueError('invalid evidence length')
        digest = run_remote(['sudo', '-n', 'sha256sum', path], timeout=60).stdout.decode().split()[0]
        if hashlib.sha256(value).hexdigest() != digest:
            raise ValueError('evidence changed during capture')
        raw[name] = value
    if hashlib.sha256(raw['preflight.json']).hexdigest() != PREFLIGHT_SHA or hashlib.sha256(raw['journal.jsonl']).hexdigest() != JOURNAL_SHA:
        raise ValueError('original stage evidence differs')
    preflight = json.loads(raw['preflight.json'])
    expected = preflight['expected']
    request = json.loads((PREPARATION / 'request.json').read_bytes())
    if not (request['relative_path'] == expected['relative_path'] and request['expected_source']['sha256'] == expected['sha256']
            and request['expected_source']['identity'][2] == expected['size']
            and request['expected_source']['identity'][3] == expected['mtime_ns']):
        raise ValueError('original source request differs')
    result = validate(expected, preflight, raw['journal.jsonl'], raw['commit.jsonl'],
                      json.loads(raw['fence.json']), json.loads(raw['readback.json']))
    out = PREPARATION / 'captured-gws-evidence'
    out.mkdir(mode=0o700)
    for name, value in raw.items():
        save(out / name, value)
    result.update(service_identity=identity, recovered_stage=True,
                  evidence_sha256={name: hashlib.sha256(value).hexdigest() for name, value in raw.items()})
    save(out / 'verification.json', (json.dumps(result, sort_keys=True, indent=2) + '\n').encode())
    fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return result
