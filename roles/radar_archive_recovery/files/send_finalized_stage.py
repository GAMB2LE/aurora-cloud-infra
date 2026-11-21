#!/usr/bin/env python3
"""Stream a finalized stage to a separately guarded destination worker.

The caller must authenticate admission, hold publication locks, and verify the
destination. A sender receipt by itself grants no publication or deletion right.
"""
import argparse
import json
import sys

from read_stage_handoff import read
from stream_protected_stage import stream_version

ROOT = '/var/lib/aurora-radar-fast-staging'


def send(root, operation, after, expected, output, *, owner=0, clock=None):
    handoff = read(root, operation, after, owner=owner, now=clock() if clock else None)
    required = {'relative_path': handoff['source'], 'size': handoff['size'],
                'mtime_ns': handoff['source_mtime_ns'], 'sha256': handoff['sha256']}
    if expected != required:
        raise ValueError('admission version differs from the root-owned handoff')
    source = {key: required[key] for key in ('relative_path', 'size', 'sha256')}
    receipt = stream_version(root, operation, source, handoff['stage_identity'], output, owner=owner)
    if read(root, operation, after, owner=owner, now=clock() if clock else None) != handoff:
        raise ValueError('handoff changed while streaming')
    return {**receipt, 'stage_operation': operation, 'post_stage_operation': after,
            'finalization_invocation_id': handoff['finalization_invocation_id']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation')
    parser.add_argument('after')
    parser.add_argument('expected_json')
    args = parser.parse_args()
    result = send(ROOT, args.operation, args.after, json.loads(args.expected_json), sys.stdout.buffer)
    print(json.dumps(result, sort_keys=True), file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
