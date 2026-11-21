#!/usr/bin/env python3
"""Publish read-only metadata from an authenticated local finalization.

This provides a handoff to the existing unprivileged transfer account. It does
not admit a transfer or authorize archive retention or eviction.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

from finalize_from_journal import validated_structure
from inspect_protected_stage import inspect
from stage_from_journal import STAGE_ROOT
from windows_native_probe import read_state


def build_evidence(operation, after, *, now):
    if not re.fullmatch('[0-9a-f]{32}', operation) or not re.fullmatch('[0-9a-f]{32}', after):
        raise ValueError('invalid stage operation')
    proof, readback = inspect(STAGE_ROOT, operation)
    final = read_state(STAGE_ROOT / operation / ('attested-finalization-evidence-' + after + '.json'))
    structure = validated_structure(operation)
    if (final.get('schema') != 'radar-native-finalized-evidence-v1'
            or final.get('stage_operation') != operation
            or final.get('post_stage_operation') != after
            or any(final.get(key) != proof.get(key) for key in ('source', 'successor', 'size', 'sha256', 'stage_path', 'stage_identity'))
            or final.get('samples') != structure.get('samples')
            or any(final.get(key) is not False for key in ('fast_archive_authorized', 'retention_authorized', 'eviction_authorized'))
            or type(final.get('checked_at')) is not int or not 0 <= now - final['checked_at'] <= 300
            or type(final.get('post_stage_observed_at')) is not int
            or not 0 <= now - final['post_stage_observed_at'] <= 300
            or readback.get('protected') is not True):
        raise ValueError('fresh authenticated finalization does not match the protected stage')
    ids = [final.get(key) for key in ('first_operation', 'second_operation', 'post_stage_operation')]
    if any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value) for value in ids):
        raise ValueError('invalid source observation identities')
    unit = 'aurora-radar-finalize-' + operation + '-' + after + '.service'
    fields = subprocess.run(['systemctl', 'show', unit, '-p', 'ExecStart', '-p', 'ExecMainPID',
                             '-p', 'InvocationID', '-p', 'Result', '-p', 'ExecMainStatus', '-p', 'ActiveState'],
                            check=True, capture_output=True, text=True, timeout=30).stdout
    props = dict(line.split('=', 1) for line in fields.splitlines() if '=' in line)
    argv = ['/usr/bin/python3', '/var/lib/aurora-radar-fast-finalization-v4/finalize_from_journal.py',
            '--first', ids[0], '--second', ids[1], '--after', ids[2], '--operation', operation]
    if (props.get('Result') != 'success' or props.get('ExecMainStatus') != '0' or props.get('ActiveState') != 'active'
            or not re.fullmatch('[0-9a-f]{32}', props.get('InvocationID', ''))
            or not props.get('ExecMainPID', '').isdigit() or int(props['ExecMainPID']) <= 0
            or 'argv[]=' + shlex.join(argv) + ' ;' not in props.get('ExecStart', '')):
        raise ValueError('finalization service identity or result differs')
    return {'schema': 'radar-protected-stage-handoff-v1', 'stage_operation': operation, 'post_stage_operation': after,
            'source': proof['source'], 'size': proof['size'], 'sha256': proof['sha256'],
            'source_mtime_ns': proof['source_identity'][3], 'stage_identity': proof['stage_identity'],
            'checked_at': final['checked_at'], 'post_stage_observed_at': final['post_stage_observed_at'],
            'finalization_invocation_id': props['InvocationID'],
            'fast_archive_authorized': False, 'retention_authorized': False, 'eviction_authorized': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--after', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('root is required')
    evidence = build_evidence(args.operation, args.after, now=int(time.time()))
    directory = os.open(STAGE_ROOT / args.operation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open('delivery-handoff-' + args.after + '.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o440, dir_fd=directory)
        os.fchown(fd, 0, os.fstat(directory).st_gid)
        os.fchmod(fd, 0o440)
        with os.fdopen(fd, 'w') as stream:
            json.dump(evidence, stream, sort_keys=True, separators=(',', ':'))
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
    print(json.dumps(evidence, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
