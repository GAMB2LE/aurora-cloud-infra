"""Reserve one fresh GWS operation under both archive writer locks.

The caller owns both lock descriptors and invokes the resulting worker only
after this function has committed a durable intent. Nothing here authorizes
deletion, retention release, or an object-store claim.
"""
import json

from gws_object_isolation import check
from gws_pending_replay import classify_pending, valid_expected
from per_file_coordinator import BASE, LEDGER, PENDING

MAX_PENDING_FILES = 256
MAX_PENDING_BYTES = 16 * 1024**3


def reserve(fresh, frozen, expected, driver, *, lock_fds, base=BASE, pending=PENDING, ledger=LEDGER):
    if not valid_expected(expected) or not expected['relative_path'].endswith('.LV1'):
        raise ValueError('invalid finalized LV1 candidate')
    if fresh.outstanding() is not None:
        raise ValueError('fresh queue already has an unresolved operation')
    other = frozen.outstanding()
    if other is not None and other.get('target') != 'object-verified':
        raise ValueError('historical GWS, pair or checkpoint operation is unresolved')
    # The check authenticates both held lock descriptors, all relevant fences,
    # and any outstanding GWS-sourced object worker against its durable marker.
    proof = check(expected, other, lock_fds=lock_fds, base=base)
    if proof.get('object_isolated') is not True:
        raise ValueError('object worker is not isolated')
    for row in frozen.db.execute('SELECT identity FROM files'):
        if json.loads(row[0]).get('relative_path') == expected['relative_path']:
            raise ValueError('candidate already belongs to the historical queue')
    versions = {}
    paired = set()
    for row in classify_pending(pending, ledger):
        version = row.get('expected')
        if not valid_expected(version) or row.get('status') not in {
                'object-copy-needed', 'pair-reconciliation-needed', 'provisional-pair-recorded'}:
            raise ValueError('global pending inventory is invalid')
        identity = json.dumps(version, sort_keys=True)
        if row['status'] == 'provisional-pair-recorded':
            paired.add(identity)
        else:
            versions[identity] = version
    for row in fresh.db.execute('SELECT identity,stage FROM files WHERE stage > 0 AND stage < 4'):
        version = json.loads(row[0])
        if not valid_expected(version): raise ValueError('fresh queue inventory is invalid')
        identity = json.dumps(version, sort_keys=True)
        # The GWS lane retains its stage-1 history after the pair ledger has
        # independently recorded both archives. Count only unresolved exact
        # versions; a different version of the same path still consumes space.
        if identity not in paired:
            versions[identity] = version
    if (any(item['relative_path'] == expected['relative_path'] for item in versions.values())
            or len(versions) + 1 > MAX_PENDING_FILES
            or sum(item['size'] for item in versions.values()) + expected['size'] > MAX_PENDING_BYTES):
        raise ValueError('candidate conflicts with or exceeds the global GWS-only backlog')
    # Revalidate worker and fences immediately before the durable intent.
    if check(expected, other, lock_fds=lock_fds, base=base) != proof:
        raise ValueError('object isolation changed during admission')
    fresh.add(expected)
    intent = driver.reserve(expected, 'gws-verified')
    operation = fresh.begin(expected, 'gws-verified', intent)
    return operation
