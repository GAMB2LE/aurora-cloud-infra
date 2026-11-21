"""Import verified single-object evidence into an additive ledger table.

This table never counts as dual archive acceptance or eviction authority.
Caller must pin inputs, serialize writes and publish independently verified
ledger snapshots before relying on durability.
"""
import datetime,hashlib,json,re,sqlite3
from pathlib import Path

BASE='gamb2le-object-store:gamb2le-o/data/internal/aurora-cloud/radar-archive-first/versions/v1/'
PATH=re.compile(r'Y\d{4}/M\d{2}/D\d{2}/[A-Za-z0-9_.-]+\.(?:LV0|LV0\.NC|LV1|LV1\.NC)\Z')
SHA=re.compile('[0-9a-f]{64}\\Z')


def valid_stream(receipt, expected, key):
    stream = receipt.get('stream')
    if not (isinstance(stream, dict) and stream.get('object_key') == key
            and all(stream.get(k) == v for k, v in expected.items())
            and stream.get('eviction_authorized') is False
            and stream.get('retention_authorized') is False):
        return False
    if stream.get('phase') == 'stream-complete':
        return ('landed_recovery' not in receipt and stream.get('independent_object_readback') is False
                and stream.get('atomic_no_replace_proven') is False
                and stream.get('immutable_backing_proven') is False)
    recovery = receipt.get('landed_recovery', {})
    readback = recovery.get('readback', {})
    return (stream.get('phase') == 'outcome-uncertain' and stream.get('source_exit') == 0
            and type(stream.get('destination_exit')) is int and stream['destination_exit'] != 0
            and recovery.get('schema') == 'radar-landed-object-recovery-v1'
            and readback.get('expected') == expected and readback.get('object_key') == key
            and readback.get('metadata_before') == readback.get('metadata_after') == receipt.get('object_metadata')
            and readback.get('readback') == receipt.get('independent_readback')
            and all(row.get(flag) is False for row in (recovery, readback)
                    for flag in ('eviction_authorized', 'retention_authorized')))


def import_receipt(db,receipt_raw,receipt_sha,request_raw,request_sha,writer_raw,writer_sha,recorded_at):
    if not all(isinstance(x,str) and SHA.fullmatch(x) for x in (receipt_sha,request_sha,writer_sha)):
        raise ValueError('invalid evidence hash')
    if hashlib.sha256(receipt_raw).hexdigest()!=receipt_sha or hashlib.sha256(request_raw).hexdigest()!=request_sha or hashlib.sha256(writer_raw).hexdigest()!=writer_sha:
        raise ValueError('evidence hash differs')
    v=json.loads(receipt_raw);request=json.loads(request_raw)
    if not (isinstance(v,dict) and v.get('schema')=='radar-direct-object-version-v1'
            and v.get('gws_verified') is False and v.get('atomic_no_replace_proven') is False
            and v.get('immutable_backing_proven') is False and v.get('eviction_authorized') is False
            and v.get('retention_authorized') is False):raise ValueError('invalid provisional receipt')
    expected=v.get('expected');identity=v.get('source_identity');metadata=v.get('object_metadata');read=v.get('independent_readback');stream=v.get('stream')
    if not (isinstance(expected,dict) and set(expected)=={'relative_path','size','mtime_ns','sha256'}
            and isinstance(expected['relative_path'],str) and PATH.fullmatch(expected['relative_path'])
            and type(expected['size']) is int and 0<expected['size']<=4*1024**3
            and type(expected['mtime_ns']) is int and expected['mtime_ns']>=0
            and isinstance(expected['sha256'],str) and SHA.fullmatch(expected['sha256'])
            and isinstance(identity,list) and len(identity)==9 and all(type(x) is int and x>=0 for x in identity)
            and identity[2]==expected['size'] and identity[3]==expected['mtime_ns']
            and isinstance(request,dict) and request.get('relative_path')==expected['relative_path']
            and isinstance(request.get('expected_source'),dict)
            and request['expected_source'].get('identity',[])[:9]==identity
            and request['expected_source'].get('sha256')==expected['sha256']):raise ValueError('source evidence differs')
    key=v.get('object_key');pattern=re.escape(BASE)+r'[0-9a-f]{32}/'+expected['sha256']+r'\.radar\Z'
    if not (isinstance(key,str) and re.fullmatch(pattern,key) and isinstance(metadata,dict)
            and metadata.get('key')==key and metadata.get('size')==expected['size']
            and isinstance(metadata.get('etag'),str) and metadata['etag']
            and isinstance(metadata.get('last_modified'),str) and metadata['last_modified']
            and isinstance(read,dict) and read.get('sha256')==expected['sha256'] and read.get('read_bytes')==expected['size']
            and valid_stream(v, expected, key)):raise ValueError('object evidence differs')
    try:
        if not isinstance(recorded_at,str) or not recorded_at.endswith('Z'):raise ValueError
        stamp=datetime.datetime.fromisoformat(recorded_at.replace('Z','+00:00'))
        if stamp.utcoffset()!=datetime.timedelta(0):raise ValueError
    except ValueError:raise ValueError('invalid recording time') from None
    db.execute('''CREATE TABLE IF NOT EXISTS provisional_object_versions (
        receipt_sha256 TEXT PRIMARY KEY, relative_path TEXT NOT NULL, size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL, content_sha256 TEXT NOT NULL, object_key TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, writer_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL,
        receipt_json BLOB NOT NULL, request_json BLOB NOT NULL)''')
    values=(receipt_sha,expected['relative_path'],expected['size'],expected['mtime_ns'],expected['sha256'],key,
            request_sha,writer_sha,recorded_at,receipt_raw,request_raw)
    with db:
        db.execute('INSERT OR IGNORE INTO provisional_object_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)',values)
        row=db.execute('SELECT * FROM provisional_object_versions WHERE receipt_sha256=?',(receipt_sha,)).fetchone()
        if tuple(row)!=values:raise ValueError('provisional receipt conflicts with ledger')
    return {'receipt_sha256':receipt_sha,'object_key':key,'object_verified':True,'gws_verified':False,
            'immutable_backing_proven':False,'eviction_authorized':False,'retention_authorized':False}
