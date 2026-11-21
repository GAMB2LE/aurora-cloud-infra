"""Close an unpublished staged attempt superseded by the existing dispatcher.

The failed attempt never gains an archive stage. Its journals and partial remain
preserved; actual canonical readbacks are recorded separately in its closure.
"""
import hashlib,json,os,re,stat,time
from pathlib import Path
from contextlib import closing
from per_file_queue import Queue,AUTHORITY,packed
SCHEMA='radar-staged-gws-delegated-to-primary-v1'
MARKER='primary-dispatch-attempt-resolution.json'


def validate(operation,receipt):
    if (operation.get('target')!='gws-verified' or receipt.get('schema')!=SCHEMA
        or receipt.get('operation_id')!=operation.get('id')
        or receipt.get('expected')!=operation.get('expected')
        or receipt.get('unit')!=operation.get('intent',{}).get('unit')
        or receipt.get('intent_sha256')!=hashlib.sha256(packed(operation['intent']).encode()).hexdigest()
        or receipt.get('worker_exit_code')!=1 or receipt.get('verified') is not False
        or receipt.get('transfer_started') is not True or receipt.get('canonical_publication_by_attempt') is not False
        or receipt.get('reason')!='canonical-published-by-primary-dispatcher'
        or any(receipt.get(k) is not True for k in ('worker_terminal','commit_journal_absent',
            'fence_journal_absent','readback_journal_absent','partial_preserved',
            'primary_sha256_verified','gws_sha256_verified','object_get_sha256_verified','dispatcher_exact_dual_delivered'))
        or any(receipt.get(k) is not v for k,v in AUTHORITY.items())
        or any(not isinstance(receipt.get(k),str) or not re.fullmatch('[0-9a-f]{64}',receipt[k])
               for k in ('worker_log_sha256','request_sha256','transfer_journal_sha256','preflight_sha256'))
        or not re.fullmatch('[0-9a-f]{32}',receipt.get('worker_invocation_id',''))):
        raise ValueError('staged delegation does not bind an independently verified superseded attempt')
    return receipt


def retire(queue,operation,receipt):
    validate(operation,receipt)
    with queue.db:
        queue.db.execute('BEGIN IMMEDIATE')
        if queue.outstanding()!=operation:raise ValueError('superseded attempt is no longer outstanding')
        row=queue.db.execute('SELECT stage FROM files WHERE identity=?',(packed(operation['expected']),)).fetchone()
        if row is None or row['stage']!=0:raise ValueError('delegation cannot alter an advanced archive stage')
        queue.db.execute('UPDATE operations SET finished=?,receipt=? WHERE id=?',(time.time(),packed(receipt),operation['id']))


def resolved_version(preparation,database):
    preparation=Path(preparation);marker=preparation/MARKER
    if not os.path.lexists(marker):return None
    fd=os.open(marker,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_nlink!=1:
            raise ValueError('delegation resolution is not protected')
        proof=json.load(stream)
    with closing(Queue(database,readonly=True)) as queue:
        row=queue.db.execute('SELECT * FROM operations WHERE id=?',(proof.get('operation_id'),)).fetchone()
    if row is None or row['finished'] is None or row['target']!=1:
        raise ValueError('delegation resolution has no closed GWS operation')
    expected,intent,receipt=(json.loads(row[k]) for k in ('identity','intent','receipt'))
    validate({'id':row['id'],'expected':expected,'intent':intent,'target':'gws-verified'},receipt)
    if proof!=receipt or Path(intent['preparation']['out'])!=preparation:
        raise ValueError('delegation resolution differs from durable attempt')
    if hashlib.sha256((preparation/'request.json').read_bytes()).hexdigest()!=receipt['request_sha256']:
        raise ValueError('delegation request changed')
    return expected
