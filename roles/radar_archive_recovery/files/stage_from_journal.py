#!/usr/bin/env python3
"""Stage one version from two protected ASS-local Windows task captures.

The operation is additive and never authorizes an archive transfer. Existing
operation directories are not reused; interrupted stages need reconciliation.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import time

from stage_native_version import stage
from windows_native_probe import read_state
from windows_observation_gate import staging_eligibility_from_captures

STATE_ROOT = Path('/var/lib/aurora-radar-windows-probes')
SOURCE_ROOT = Path('/home/aurora/data/rpgfmcw94')
STAGE_ROOT = Path('/var/lib/aurora-radar-fast-staging')
MINIMUM_FREE = 6 * 1024**3


def load_capture(root, operation, *, owner=0):
    if not isinstance(operation, str) or not re.fullmatch('[0-9a-f]{32}', operation):
        raise ValueError('invalid Windows observation operation')
    root = Path(root)
    for path in (root, root / operation):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o077:
            raise ValueError('Windows observation journal is not protected')
    directory = root / operation
    capture = {key: read_state(directory / (key + '.json'))
               for key in ('intent', 'observation', 'attestation', 'complete')}
    if capture['intent'].get('operation') != operation:
        raise ValueError('Windows observation journal identity differs')
    return capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--first', required=True)
    parser.add_argument('--second', required=True)
    parser.add_argument('--operation', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('root is required for protected staging')
    captures = [load_capture(STATE_ROOT, args.first), load_capture(STATE_ROOT, args.second)]
    eligibility = staging_eligibility_from_captures(*captures, now=int(time.time()))
    result = stage(SOURCE_ROOT, STAGE_ROOT, eligibility, args.operation,
                   captures=captures, minimum_free_bytes=MINIMUM_FREE)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
