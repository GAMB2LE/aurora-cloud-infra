"""Journal one provisional source-to-object archive version, without a spool.

A reviewed adapter must supply locks, fresh source/capacity/hold checks, exact
object-key absence, pinned commands and independent object readback. Random
keys are not immutable and this operation grants no deletion authority.
"""
import hashlib,json,os,secrets,time
from pathlib import Path
import direct_backlog_recovery as shared
import direct_object_gzip_pipeline as transport
import direct_interrupt


def transfer_one(adapter,observation,journal_path,*,clock=time.time,monotonic=time.monotonic):
    journal=Path(journal_path);preflight=Path(str(journal)+'.preflight.json');readback=Path(str(journal)+'.readback.json')
    shared._validate_observation(observation,clock())
    with direct_interrupt.protect(),adapter.exclusive_recovery_locks():
        deadline=monotonic()+21600;adapter.set_deadline(deadline)
        def remaining():
            value=deadline-monotonic()
            if value<=0:raise TimeoutError('object transfer budget exhausted')
            return value
        if any(os.path.lexists(p) for p in (preflight,journal,readback)):raise ValueError('existing attempt requires reconciliation')
        adapter.require_controls_capacity_and_hold()
        adapter.require_source_observation(observation)
        source=adapter.hash_settled_source(observation['relative_path'],timeout=remaining());shared._validate_source(observation,source)
        shared._validate_observation(observation,clock());adapter.require_source_observation(observation);adapter.require_source_identity(source)
        nonce=secrets.token_hex(16);expected={'relative_path':observation['relative_path'],'size':observation['size'],
             'mtime_ns':source['identity'][3],'sha256':source['sha256']}
        key=transport.OBJECT_BASE+'/'+nonce+'/'+source['sha256']+'.radar'
        absence=adapter.require_exact_object_absence(key,timeout=remaining())
        if not (isinstance(absence,dict) and absence.get('key')==key and absence.get('absent') is True and absence.get('positive_control') is True):
            raise ValueError('object absence not proven')
        adapter.require_controls_capacity_and_hold();adapter.require_source_identity(source)
        sender,dest,source_raw=adapter.object_commands(expected,source['identity'],key)
        if not isinstance(source_raw,bytes):raise ValueError('unpinned source script')
        absence_raw=json.dumps(absence,sort_keys=True,separators=(',',':')).encode()
        operation={'nonce':nonce,'source_script_sha256':hashlib.sha256(source_raw).hexdigest(),
                   'absence_sha256':hashlib.sha256(absence_raw).hexdigest(),'object_key':key}
        shared._save_exclusive(preflight,{'schema':'radar-direct-object-preflight-v1','observation':observation,
              'source':source,'expected':expected,'absence':absence,'operation':operation,
              'atomic_no_replace_proven':False,'eviction_authorized':False,'retention_authorized':False})
        stream=transport.transfer(sender,dest,expected,operation,journal,timeout=remaining(),safety_check=lambda:adapter.require_stream_safety(expected['size']))
        adapter.require_stream_safety(expected['size'])
        adapter.require_source_identity(source)
        first=adapter.object_metadata(key,timeout=remaining())
        if not isinstance(first,dict) or first.get('size')!=expected['size'] or first.get('key')!=key:
            raise ValueError('object metadata differs')
        read=adapter.independent_object_readback(key,expected['size'],timeout=remaining())
        second=adapter.object_metadata(key,timeout=remaining())
        if first!=second or not isinstance(read,dict) or read.get('sha256')!=expected['sha256'] or read.get('read_bytes')!=expected['size']:
            raise ValueError('object readback differs')
        adapter.require_source_identity(source);remaining()
        receipt={'schema':'radar-direct-object-version-v1','expected':expected,'source_identity':source['identity'],
                 'object_key':key,'object_metadata':first,'independent_readback':read,'stream':stream,
                 'gws_verified':False,'atomic_no_replace_proven':False,'immutable_backing_proven':False,
                 'eviction_authorized':False,'retention_authorized':False}
        shared._save_exclusive(readback,receipt)
        return {'object_key':key,'size':expected['size'],'object_verified':True,'gws_verified':False,
                'immutable_backing_proven':False,'eviction_authorized':False,'retention_authorized':False}
