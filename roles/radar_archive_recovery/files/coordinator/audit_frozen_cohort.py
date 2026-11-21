#!/usr/bin/env python3
"""Reconcile the frozen repair obligations against the working archive ledger."""
import argparse
from collections import Counter
import contextlib
import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

from gws_only_runner import exclusive_json
from gws_pending_replay import valid_expected
from per_file_queue import AUTHORITY, writer_lock
from prepare_direct_candidate import BASE
from reconcile_gws_first_pair import check_retention_and_fence
from verify_object_version_live import main as verify_object
from verify_pending_gws_live import main as verify_gws


def read(path):
    raw = path.read_bytes()
    result = json.loads(raw)
    if any(result.get(key) is not value for key, value in AUTHORITY.items()):
        raise ValueError(f'{path.name} has unexpected authority flags')
    return raw, result


def obligations(queue_path, interrupted_path):
    queue_raw, queue = read(queue_path)
    handoff_raw, handoff = read(interrupted_path)
    candidates = queue['candidates']
    if len(candidates) != queue['candidate_count']:
        raise ValueError('candidate count differs')
    result = {}
    for row in candidates:
        path = row['relative_path']
        if path in result:
            raise ValueError('duplicate frozen candidate')
        result[path] = {**row, 'origin':'frozen-inventory'}
    for item in handoff['items']:
        expected = item['expected']
        if not valid_expected(expected):
            raise ValueError('invalid interrupted version')
        path = expected['relative_path']
        if path in result and (result[path]['size'], result[path]['mtime']) != (
                expected['size'], expected['mtime_ns']//10**9):
            raise ValueError('frozen and interrupted versions disagree')
        result[path] = {**expected, 'mtime':expected['mtime_ns']//10**9,
                        'origin':'interrupted-batch'}
    return result, {
        'queue_sha256':hashlib.sha256(queue_raw).hexdigest(),
        'interrupted_batch_sha256':hashlib.sha256(handoff_raw).hexdigest(),
    }


def version_rows(connection, candidate):
    path, size, mtime = (candidate[key] for key in ('relative_path','size','mtime'))
    rows = connection.execute(
        '''SELECT receipt_sha256,mtime_ns,content_sha256,object_key,receipt_json
           FROM provisional_gws_first_pairs
           WHERE relative_path=? AND size=? AND mtime_ns BETWEEN ? AND ?''',
        (path,size,mtime*10**9,(mtime+1)*10**9-1),
    ).fetchall()
    result = []
    for digest, ns, sha, key, raw in rows:
        expected = {'relative_path':path,'size':size,'mtime_ns':ns,'sha256':sha}
        if 'sha256' in candidate and (ns,sha) != (candidate['mtime_ns'],candidate['sha256']):
            continue
        pair = json.loads(raw)
        if (not valid_expected(expected) or hashlib.sha256(raw).hexdigest() != digest
                or pair.get('schema') != 'radar-gws-first-pair-v1'
                or pair.get('expected') != expected or pair.get('object_key') != key
                or any(pair.get(k) is not v for k,v in AUTHORITY.items())):
            raise ValueError('pair ledger receipt differs')
        object_receipt = pair['object_receipt_sha256']
        attached = connection.execute(
            '''SELECT receipt_json FROM provisional_object_versions WHERE receipt_sha256=?
               AND relative_path=? AND size=? AND mtime_ns=? AND content_sha256=? AND object_key=?''',
            (object_receipt,path,size,ns,sha,key),
        ).fetchall()
        if len(attached) != 1 or hashlib.sha256(attached[0][0]).hexdigest() != object_receipt:
            raise ValueError('pair lacks its exact object receipt')
        result.append({'expected':expected, 'object_receipt_sha256':object_receipt,
                       'pair_receipt_sha256':digest})
    return result


def ledger_report(items):
    ledger = BASE/'working-ledger.sqlite'
    with contextlib.closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)) as connection:
        connection.execute('BEGIN')
        if connection.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('working ledger integrity failed')
        report = []
        for path,item in sorted(items.items()):
            versions = version_rows(connection,item)
            if len(versions) == 1:
                report.append({'relative_path':path,'state':'pair-recorded',**versions[0]})
            else:
                report.append({'relative_path':path,'state':'missing-pair' if not versions else 'ambiguous-pair'})
        return report


def readback(report):
    for row in report:
        if row['state'] != 'pair-recorded':
            continue
        expected = row['expected']
        gws_path,gws = verify_gws(expected['relative_path'])
        object_path,obj = verify_object(row['object_receipt_sha256'])
        if gws['expected'] != expected or obj['expected'] != expected:
            raise ValueError('readback differs from frozen obligation')
        row.update(state='live-pair-verified',gws_evidence=str(gws_path),object_evidence=str(object_path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('queue',type=Path)
    parser.add_argument('interrupted',type=Path)
    parser.add_argument('--check-bytes',action='store_true')
    args = parser.parse_args()
    items, digests = obligations(args.queue,args.interrupted)
    if args.check_bytes:
        with writer_lock(BASE/'per-file-coordinator.lock'), writer_lock(BASE/'bounded-backlog-coordinator.lock'):
            check_retention_and_fence()
            report = ledger_report(items)
            if all(row['state'] == 'pair-recorded' for row in report):
                readback(report)
            check_retention_and_fence()
    else:
        report = ledger_report(items)
    counts = Counter(row['state'] for row in report)
    now = datetime.datetime.now(datetime.timezone.utc)
    summary = {'schema':'radar-frozen-cohort-audit-v1','at':now.isoformat(),
               'obligations':len(items),'counts':dict(counts),**digests,**AUTHORITY}
    path = BASE/now.strftime('frozen-cohort-audit-%Y%m%dT%H%M%SZ.json')
    exclusive_json(path,{**summary,'files':report})
    print(json.dumps({**summary,'report':str(path)},sort_keys=True))


if __name__ == '__main__':
    main()
