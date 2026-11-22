"""Pure release checks shared by the scheduled retention window and its tests."""
import datetime as dt

COUNTERS = ('retention_local_missing_count', 'retention_local_mismatch_count',
            'retention_gws_missing_count', 'retention_gws_mismatch_count')


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('verification timestamp lacks timezone')
    return result


def require_ready(gate, summary, stream_names, now):
    raw = gate.get('domains', {}).get('raw_retention', {})
    if (gate.get('raw_retention_ready') is not True
            or raw.get('stable_parity') is not True
            or raw.get('complete_verification') is not True
            or raw.get('failures') != []
            or gate.get('writers_policy') != 'independent'):
        raise ValueError('raw retention archive gate is not ready')
    for stamp in (raw.get('evidence_floor_generated_at'), summary.get('generated_at')):
        if not isinstance(stamp, str):
            raise ValueError('verification timestamp is missing')
        age = now - timestamp(stamp)
        if not dt.timedelta(0) <= age < dt.timedelta(hours=8):
            raise ValueError('verification evidence is stale or from the future')
    for name in stream_names:
        stream = summary.get('streams', {}).get(name, {})
        if stream.get('error') or any(type(stream.get(k)) is not int or stream[k] != 0 for k in COUNTERS):
            raise ValueError('retention-age mirror evidence is incomplete: ' + name)


def require_receipts(receipts, expected):
    paths = []
    expected_paths = {row['path'] for row in expected}
    if len(expected_paths) != len(expected):
        raise ValueError('duplicate selected source paths')
    for receipt in receipts:
        batch = receipt.get('paths')
        if (receipt.get('site') != 'ass' or receipt.get('dry_run') is not False
                or not isinstance(batch, list) or receipt.get('candidate_count') != len(batch)):
            raise ValueError('invalid signed deletion receipt')
        paths.extend(batch)
    if len(paths) != len(set(paths)) or set(paths) != expected_paths:
        raise ValueError('signed deletion paths differ from the selected candidates')


def require_absent(observed, expected):
    paths = {row['path'] for row in expected}
    if set(observed) != paths or any(value is not None for value in observed.values()):
        raise ValueError('a selected source path remains, was refilled, or was not checked')
