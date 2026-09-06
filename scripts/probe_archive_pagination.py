#!/usr/bin/env python3
"""Five bounded, read-only S3 requests; output contains no keys or credentials."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, '/usr/local/lib/aurora-object-store')
from aurora_object_store_s3 import InventoryError, _client, _request_error


class ProbeFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ProbeFailure(message)


def validate_page(response, *, bucket, prefix, max_keys, delimiter=False):
    require(isinstance(response, dict), 'listing response is not an object')
    require(type(response.get('IsTruncated')) is bool, 'listing truncation flag is invalid')
    require(response.get('Name') == bucket and response.get('Prefix') == prefix,
            'listing response does not match the requested bucket and prefix')
    objects = response.get('Contents', [])
    prefixes = response.get('CommonPrefixes', [])
    require(isinstance(objects, list) and isinstance(prefixes, list), 'listing arrays are malformed')
    count = response.get('KeyCount')
    require(type(count) is int and count == len(objects) + len(prefixes) and 0 <= count <= max_keys,
            'listing KeyCount does not match objects and common prefixes')
    next_token = response.get('NextContinuationToken')
    if response['IsTruncated']:
        require(isinstance(next_token, str) and bool(next_token), 'truncated listing has no continuation token')
    else:
        require(next_token is None, 'terminal listing unexpectedly has a continuation token')
    keys = set()
    for item in objects:
        require(isinstance(item, dict), 'object metadata is malformed')
        key, size, modified = item.get('Key'), item.get('Size'), item.get('LastModified')
        require(isinstance(key, str) and key.startswith(prefix) and key not in keys,
                'listing contains an unrelated or duplicate object')
        require(type(size) is int and size >= 0, 'object size metadata is invalid')
        require(isinstance(modified, dt.datetime) and modified.tzinfo is not None,
                'object timestamp metadata is invalid')
        if delimiter:
            require('/' not in key[len(prefix):].rstrip('/'), 'delimiter listing returned a nested object')
        keys.add(key)
    require(delimiter or not prefixes, 'recursive listing returned common prefixes')
    seen_prefixes = set()
    for item in prefixes:
        require(isinstance(item, dict), 'common prefix metadata is malformed')
        child = item.get('Prefix')
        require(isinstance(child, str) and child.startswith(prefix) and child != prefix and
                child.endswith('/') and '/' not in child[len(prefix):-1] and child not in seen_prefixes,
                'delimiter listing contains an invalid common prefix')
        seen_prefixes.add(child)
    return keys, seen_prefixes


def run_probe(config, client):
    products = next((job for job in config['jobs'] if job['name'] == 'products'), None)
    require(products is not None, 'products family is absent from the catalogue')
    base = products['destination'].strip('/') + '/'
    store = base + 'cl61/gamb2le_depolarisation_lidar_ceilometer_aurora.zarr/'
    prefix = store + 'longitude/'
    bucket = config['bucket']
    first = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=2)
    first_keys, _ = validate_page(first, bucket=bucket, prefix=prefix, max_keys=2)
    require(first['IsTruncated'] and first_keys, 'probe prefix must contain at least three objects')
    second = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=2,
                                    ContinuationToken=first['NextContinuationToken'])
    second_keys, _ = validate_page(second, bucket=bucket, prefix=prefix, max_keys=2)
    require(second_keys and first_keys.isdisjoint(second_keys), 'pagination repeated or omitted the next page')
    require(second.get('NextContinuationToken') != first['NextContinuationToken'], 'pagination token did not advance')

    shallow = client.list_objects_v2(Bucket=bucket, Prefix=store, Delimiter='/', MaxKeys=1000)
    shallow_keys, common_prefixes = validate_page(shallow, bucket=bucket, prefix=store, max_keys=1000, delimiter=True)
    require(prefix in common_prefixes, 'delimiter discovery did not return the known longitude child')

    # The first sorted array metadata key is a small, existing exact prefix.
    # This validates the real endpoint's terminal-page shape without scanning
    # the complete high-cardinality array.
    terminal_prefix = min(first_keys)
    terminal = client.list_objects_v2(Bucket=bucket, Prefix=terminal_prefix, MaxKeys=1000)
    terminal_keys, _ = validate_page(terminal, bucket=bucket, prefix=terminal_prefix, max_keys=1000)
    require(not terminal['IsTruncated'] and terminal_prefix in terminal_keys,
            'existing-object prefix did not return a complete terminal page')

    empty_prefix = base + '.aurora-read-only-probe-' + uuid.uuid4().hex + '/'
    empty = client.list_objects_v2(Bucket=bucket, Prefix=empty_prefix, MaxKeys=1000)
    empty_keys, _ = validate_page(empty, bucket=bucket, prefix=empty_prefix, max_keys=1000)
    require(not empty['IsTruncated'] and not empty_keys, 'random nonexistent prefix was not an empty terminal page')
    return {'compatible': True, 'requests': 5, 'recursive_pages': 2,
            'recursive_objects': len(first_keys | second_keys),
            'delimiter_objects': len(shallow_keys), 'delimiter_prefixes': len(common_prefixes),
            'terminal_objects': len(terminal_keys), 'empty_objects': 0,
            'key_count_consistent': True, 'page_metadata_valid': True,
            'addressing': config.get('s3_addressing_style', 'path'),
            'credential_source': 'systemd' if os.environ.get('CREDENTIALS_DIRECTORY') else 'restricted file'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', default='/etc/aurora-object-store/catalog.json')
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.catalog).read_text())
        result = run_probe(config, _client(config))
    except (InventoryError, ProbeFailure) as error:
        print(json.dumps({'compatible': False, 'error_class': getattr(error, 'error_class', 'compatibility'),
                          'error': str(error)}))
        return 1
    except Exception as error:
        sanitized, _ = _request_error(error, has_cursor=False)
        print(json.dumps({'compatible': False, 'error_class': sanitized.error_class, 'error': str(sanitized)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
