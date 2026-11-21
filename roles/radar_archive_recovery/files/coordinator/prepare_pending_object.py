#!/usr/bin/env python3
"""Read-only admission for an exact GWS-only version's missing object copy.

This does not launch a payload transfer, pair ledger records, or authorize
retention or deletion. An unresolved batch or held coordinator lock blocks it.
"""
import argparse
import fcntl
import json
import os
import sqlite3
import sys
from pathlib import Path

from bounded_backlog_runner import AdmissionCapacityUnavailable, admission, require_reconciled
from gws_pending_replay import require_fresh_source, verified_pending
from prepare_direct_candidate import BASE, prepare
from verify_pending_gws_live import main as verify_gws


def main(relative, *, gws_source=False):
    fd = os.open(BASE/'bounded-backlog-coordinator.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        require_reconciled(BASE)
        selected = [row for row in verified_pending(BASE/'gws-only-pending.sqlite') if row['relative_path'] == relative]
        if len(selected) != 1: raise ValueError('pending version absent or ambiguous')
        expected = selected[0]
        db = sqlite3.connect('file:'+str(BASE/'working-ledger.sqlite')+'?mode=ro', uri=True)
        try:
            rows = db.execute('SELECT object_key FROM provisional_object_versions WHERE relative_path=? AND size=? AND mtime_ns=? AND content_sha256=?',
                              (relative, expected['size'], expected['mtime_ns'], expected['sha256'])).fetchall()
        finally: db.close()
        if rows: raise ValueError('object version already recorded; reconcile the pair')
        gws_path, gws = verify_gws(relative)
        if gws.get('expected') != expected: raise ValueError('fresh GWS version differs')
        prepared = prepare(relative, gws_source=True) if gws_source else prepare(relative)
        out = Path(prepared['out'])
        request = json.loads((out/'request.json').read_bytes())
        require_fresh_source(expected, request)
        admitted = admission(prepared['admission_unit'], request, object_destination=True)
        result = {'expected': expected, 'preparation': str(out), 'admission': admitted,
                  'fresh_gws_evidence': str(gws_path), 'transfer_started': False,
                  'object_verified': False, 'eviction_authorized': False, 'retention_authorized': False}
        name = out/'gws-pending-admission.json'
        dest = os.open(name, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        with os.fdopen(dest, 'w') as f: json.dump(result, f, sort_keys=True, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
        directory = os.open(out, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: os.fsync(directory)
        finally: os.close(directory)
        print(json.dumps(result, sort_keys=True))
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('relative')
    parser.add_argument('--from-gws', action='store_true')
    args = parser.parse_args()
    try:
        main(args.relative, gws_source=args.from_gws)
    except AdmissionCapacityUnavailable as error:
        print(json.dumps({'blocker':str(error), 'transfer_started':False}, sort_keys=True))
        raise SystemExit(75) from None
