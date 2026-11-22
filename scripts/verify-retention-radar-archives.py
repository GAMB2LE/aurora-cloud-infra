#!/usr/bin/env python3
"""Read every candidate radar file from both archives; never authorize deletion."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys

BASE = Path('/var/lib/aurora-retention-return-20261121')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--day', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'Y2026/M05/D(?:02|03|04|05|24|26)', args.day):
        raise ValueError('day is outside assessed cohort')
    proof_path = BASE / (args.day.replace('/', '-') + '-source-readback.json')
    raw = proof_path.read_bytes()
    proof = json.loads(raw)
    if proof['day'] != args.day or len(proof['files']) != 96:
        raise ValueError('incomplete current source proof')
    sys.path.insert(0, '/var/lib/aurora-radar-coordinator-v1/overnight-20261114')
    from verify_native_archive_pairs_exact import CATALOG, _client, verify_pair
    client = _client(json.loads(CATALOG.read_text()))
    output = BASE / (args.day.replace('/', '-') + '-archive-readbacks')
    output.mkdir(mode=0o700, exist_ok=True)
    for index, (name, expected) in enumerate(sorted(proof['files'].items()), 1):
        if Path(name).name != name or not re.fullmatch('[a-f0-9]{64}', expected['sha256']):
            raise ValueError('unsafe or invalid proof entry')
        target = output / (name + '.json')
        if target.exists():
            saved = json.loads(target.read_text())
            if saved['expected'] != expected or saved['source_proof_sha256'] != hashlib.sha256(raw).hexdigest():
                raise ValueError('existing archive readback describes a different source')
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(saved['verified_at'].replace('Z', '+00:00'))
            if dt.timedelta(0) <= age < dt.timedelta(hours=6):
                continue
            history = output / 'history'; history.mkdir(mode=0o700, exist_ok=True)
            previous = target.read_bytes()
            history_path = history / (target.name + '-' + hashlib.sha256(previous).hexdigest())
            if not history_path.exists():
                with history_path.open('xb') as stream:
                    stream.write(previous); stream.flush(); os.fsync(stream.fileno())
        relative = args.day + '/' + name
        verify_pair(client, relative, expected['size'], expected['sha256'])
        if proof_path.read_bytes() != raw:
            raise ValueError('source proof changed during archive readback')
        record = {'schema': 'retention-radar-dual-archive-readback-v1',
                  'verified_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'relative_path': relative, 'expected': expected,
                  'source_proof_sha256': hashlib.sha256(raw).hexdigest(),
                  'gws_sha256_verified': True, 'object_get_sha256_verified': True,
                  'deletion_authorized': False}
        temporary = target.with_suffix('.tmp')
        if temporary.exists():
            temporary.rename(temporary.with_name(temporary.name + '-interrupted-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f')))
        with temporary.open('x') as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, target)
        print(json.dumps({'day': args.day, 'dual_archive_files_verified': index,
                          'last_file': name}), flush=True)
    print(json.dumps({'day': args.day, 'complete_dual_archive_files': 96,
                      'deletion_authorized': False}), flush=True)


if __name__ == '__main__':
    main()
