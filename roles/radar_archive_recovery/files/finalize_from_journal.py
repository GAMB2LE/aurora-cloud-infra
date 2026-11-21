#!/usr/bin/env python3
"""Bind protected source, stage and local parser evidence without authorizing delivery."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

from inspect_protected_stage import inspect
from native_finalization_evidence import assess_captures
from stage_from_journal import load_capture, STATE_ROOT, STAGE_ROOT
from windows_native_probe import read_state


def validated_structure(operation):
    """Require the exact successful, resource-limited local validation service."""
    unit = 'aurora-radar-lv1-validate-' + operation + '.service'
    result = read_state(STAGE_ROOT / operation / 'structure-validation.json')
    output = subprocess.run(['systemctl', 'show', unit, '-p', 'InvocationID', '-p', 'ExecMainPID',
                             '-p', 'Result', '-p', 'ExecMainStatus', '-p', 'ActiveState',
                             '-p', 'MemoryMax', '-p', 'RuntimeMaxUSec'], check=True,
                            capture_output=True, text=True, timeout=30).stdout
    props = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if (result.get('worker_unit') != unit or not re.fullmatch('[0-9a-f]{32}', result.get('worker_invocation_id', ''))
            or props.get('InvocationID') != result['worker_invocation_id']
            or props.get('ExecMainPID') != str(result.get('worker_pid'))
            or props.get('Result') != 'success' or props.get('ExecMainStatus') != '0'
            or props.get('ActiveState') != 'active' or props.get('MemoryMax') != '805306368'
            or props.get('RuntimeMaxUSec') != '10min'):
        raise ValueError('local validation service identity, result or limits differ')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('first', 'second', 'after', 'operation'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch('[0-9a-f]{32}', args.operation):
        raise ValueError('root and an exact stage operation are required')
    captures = [load_capture(STATE_ROOT, value) for value in (args.first, args.second, args.after)]
    stage, readback = inspect(STAGE_ROOT, args.operation)
    structure = validated_structure(args.operation)
    result = assess_captures(*captures, stage, structure, readback, now=int(time.time()))
    current, current_readback = inspect(STAGE_ROOT, args.operation)
    if current != stage or current_readback != readback or validated_structure(args.operation) != structure:
        raise ValueError('stage or validation changed during finalization')
    directory = os.open(STAGE_ROOT / args.operation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open('attested-finalization-evidence-' + args.after + '.json',
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'w') as stream:
            json.dump(result, stream, sort_keys=True, separators=(',', ':'))
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
