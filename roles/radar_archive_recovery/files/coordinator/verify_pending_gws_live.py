#!/usr/bin/env python3
"""Fresh read-only content check of one GWS-only pending version."""
import ast
import datetime
import json
import os
from pathlib import Path
import shlex
import sys

from gws_pending_replay import valid_expected, verified_pending
from prepare_direct_candidate import BASE, run_remote

HERE = Path(__file__).resolve().parent
GWS_ROOT = '/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/rpgfmcw94/'
GWS_ENDPOINTS = {'vm01':'rrniii@xfer-vm-01.jasmin.ac.uk',
                 'vm03':'rrniii@xfer-vm-03.jasmin.ac.uk'}


def selected_endpoint():
    choice = os.environ.get('RADAR_GWS_READBACK_ENDPOINT', 'vm01')
    if choice not in GWS_ENDPOINTS: raise ValueError('unknown GWS readback endpoint')
    return GWS_ENDPOINTS[choice]


def readback_code():
    tree = ast.parse((HERE/'direct_backlog_cloud_adapter.py').read_text())
    matches = [node.value for node in tree.body if isinstance(node, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == 'READBACK' for t in node.targets)]
    if len(matches) != 1: raise ValueError('readback implementation missing or ambiguous')
    return ast.literal_eval(matches[0])


def readback(expected):
    """Validate exact canonical bytes without writing evidence or changing workers."""
    if not valid_expected(expected): raise ValueError('invalid expected GWS version')
    endpoint = selected_endpoint()
    row = dict(expected)
    relative = row['relative_path']
    canonical_path = GWS_ROOT + relative
    remote = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
              '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no', '-o', 'IdentitiesOnly=yes',
              '-i', '/home/aurora/.ssh/id_rsa_jasmin_20200514', endpoint,
              shlex.join(['python3', '-c', readback_code(), canonical_path,
                          str(row['size']), str(row['mtime_ns']), row['sha256']])]
    proof = json.loads(run_remote(['sudo', '-n', '-u', 'aurora', '--', *remote], timeout=600).stdout)
    proof['relative_path'] = relative
    if not (all(proof.get(k) == v for k, v in row.items()) and proof.get('stable') is True):
        raise ValueError('fresh GWS proof differs')
    proof.update(endpoint=endpoint, path=canonical_path)
    now = datetime.datetime.now(datetime.timezone.utc)
    return {'checked_at': now.isoformat(), 'expected': row, 'proof': proof,
            'object_verified': False, 'immutable_backing_proven': False,
            'eviction_authorized': False, 'retention_authorized': False}


def main(relative):
    records = [row for row in verified_pending(BASE/'gws-only-pending.sqlite') if row['relative_path'] == relative]
    if len(records) != 1: raise ValueError('pending path absent or ambiguous')
    result = readback(records[0])
    now = datetime.datetime.fromisoformat(result['checked_at'])
    path = BASE/('pending-gws-live-readback-'+now.strftime('%Y%m%dT%H%M%SZ')+'.json')
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out: json.dump(result, out, sort_keys=True, indent=2); out.write('\n'); out.flush(); os.fsync(out.fileno())
    fd = os.open(BASE, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)
    print(json.dumps({'evidence': str(path), 'gws_verified': True, 'object_verified': False,
                      'eviction_authorized': False, 'retention_authorized': False}))
    return path, result


if __name__ == '__main__': main(sys.argv[1])
