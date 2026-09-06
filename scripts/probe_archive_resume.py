#!/usr/bin/env python3
"""Prove committed S3 page recovery across two isolated probe processes.

The interrupt process intentionally exits 75 after its first page commits.
Only a private probe epoch is written; archive objects and canonical evidence
are never changed. The resume process must fetch only the remaining page.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, '/usr/local/lib/aurora-object-store')
from aurora_object_store_s3 import InventoryError, PagedS3Lister, _client, _request_error


class ProbeFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ProbeFailure(message)


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class CountingClient:
    def __init__(self, client):
        self.client = client
        self.requests = []

    def list_objects_v2(self, **request):
        self.requests.append(request)
        return self.client.list_objects_v2(**request)


def run_probe(catalog, state_root, phase):
    state_root = Path(state_root)
    require(state_root.is_dir() and not state_root.is_symlink() and
            state_root.name.startswith('archive-resume-probe.'), 'probe requires its own private state directory')
    stat = state_root.stat()
    require(stat.st_uid == os.geteuid() and not stat.st_mode & 0o077,
            'probe state must be private and owned by the probe user')
    config = dict(catalog, recovery_root=str(state_root), manifest_root=str(state_root), recovery_s3_page_size=2)
    meta_path = state_root / 'probe.json'
    if phase == 'interrupt':
        require(not meta_path.exists(), 'interruption probe has already been initialized')
        metadata = {'verification_id': str(uuid.uuid4()), 'deadline': time.time() + 3600,
                    'configuration_sha256': digest(config)}
        write_json(meta_path, metadata)
    else:
        require(meta_path.is_file(), 'interruption probe metadata is missing')
        metadata = json.loads(meta_path.read_text())
        require(metadata.get('configuration_sha256') == digest(config), 'probe configuration changed before resume')
        require((state_root / 'interrupted.json').is_file(), 'first committed page interruption was not recorded')
    products = next((job for job in config['jobs'] if job['name'] == 'products'), None)
    require(products is not None, 'products family is absent from the catalogue')
    destination = products['destination'].strip('/') + '/cl61/gamb2le_depolarisation_lidar_ceilometer_aurora.zarr'
    job = {'name': 'products', 'destination': destination, 'shard_all_prefixes': True}
    # This is a discovery hint for the known three-object longitude prefix,
    # not a source inventory or a parity claim. No comparison is published.
    local = {'longitude/.zarray': {'relative_path': 'longitude/.zarray', 'size': 0, 'mtime': 0, 'checksum': ''}}
    client = CountingClient(_client(config))
    config['_recovery_deadline'] = metadata['deadline']
    epoch = state_root / 'epochs' / metadata['verification_id']
    with PagedS3Lister(config, epoch, metadata['verification_id'], client=client) as lister:
        if phase == 'interrupt':
            def interrupt_after_commit(progress):
                state = lister.db.execute('SELECT cursor, done FROM shards').fetchone()
                require(progress['pages_completed'] == 1 and progress['objects_observed'] == 2 and
                        state is not None and state['cursor'] and not state['done'] and len(client.requests) == 1,
                        'first committed page did not match the bounded probe shape')
                proof = {'phase': 'interrupted_after_commit', 'requests': 1, 'pages_completed': 1,
                         'objects_observed': 2, 'saved_cursor_present': True,
                         'verification_id': metadata['verification_id']}
                write_json(state_root / 'interrupted.json', proof)
                print(json.dumps(proof), flush=True)
                os._exit(75)
            config['_progress_callback'] = interrupt_after_commit
            lister.inventory(job, local)
            raise ProbeFailure('interruption callback did not terminate the probe process')
        state = lister.db.execute('SELECT cursor, done FROM shards').fetchone()
        before = lister.progress()
        require(state is not None and state['cursor'] and not state['done'] and
                before['pages_completed'] == 1 and before['objects_observed'] == 2,
                'saved checkpoint does not contain exactly the first complete page')
        saved_cursor = state['cursor']
        result = lister.inventory(job, local)
        after = lister.progress()
        require(len(client.requests) == 1 and client.requests[0].get('ContinuationToken') == saved_cursor,
                'resumed process did not start exclusively at the saved cursor')
        require(len(result) == 3 and after['pages_completed'] == 2 and after['objects_observed'] == 3 and
                after['shards_total'] == after['shards_completed'] == 1,
                'resumed inventory did not finish the known three-object prefix')
        proof = {'compatible': True, 'phase': 'resumed', 'requests': len(client.requests),
                 'started_with_saved_cursor': True, 'pages_completed': after['pages_completed'],
                 'objects_observed': len(result), 'verification_id': metadata['verification_id']}
        write_json(state_root / 'resumed.json', proof)
        return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--state-root', required=True)
    parser.add_argument('phase', choices=('interrupt', 'resume'))
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.catalog).read_text())
        result = run_probe(config, args.state_root, args.phase)
    except (InventoryError, ProbeFailure) as error:
        print(json.dumps({'compatible': False, 'error_class': getattr(error, 'error_class', 'probe'), 'error': str(error)}))
        return 1
    except Exception as error:
        sanitized, _ = _request_error(error, has_cursor=False)
        print(json.dumps({'compatible': False, 'error_class': sanitized.error_class, 'error': str(sanitized)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
