"""Stream a pinned field-source version to a unique object key without disk staging.

This canary transport does not provide conditional creation, immutable backing,
independent object verification, or authority to release any local data.
"""
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from direct_pipeline_bounded import _append, _json_file, _stop, _bound_child_files
from gws_receive import PATH, SHA, MAX_SIZE

OBJECT_BASE = 'gamb2le-object-store:gamb2le-o/data/internal/aurora-cloud/radar-archive-first/versions/v1'


def transfer(source_command, object_command, expected, operation, journal, *, timeout=21600, safety_check=None, check_interval=120):
    if not (isinstance(source_command, list) and source_command and all(isinstance(x, str) for x in source_command)
            and isinstance(object_command, list) and object_command and all(isinstance(x, str) for x in object_command)
            and isinstance(expected, dict) and set(expected)=={'relative_path','size','mtime_ns','sha256'}
            and isinstance(expected['relative_path'],str) and PATH.fullmatch(expected['relative_path'])
            and type(expected['size']) is int and 0 < expected['size'] <= MAX_SIZE
            and type(expected['mtime_ns']) is int and 0 <= expected['mtime_ns'] < 2**63
            and isinstance(expected['sha256'],str) and SHA.fullmatch(expected['sha256'])
            and isinstance(operation,dict) and set(operation)=={'nonce','source_script_sha256','absence_sha256','object_key'}
            and isinstance(operation['nonce'],str) and re.fullmatch('[0-9a-f]{32}',operation['nonce'])
            and all(isinstance(operation[k],str) and SHA.fullmatch(operation[k]) for k in ('source_script_sha256','absence_sha256'))
            and operation['object_key']==OBJECT_BASE+'/'+operation['nonce']+'/'+expected['sha256']+'.radar'
            and type(timeout) in (int,float) and 0 < timeout <= 21600 and (safety_check is None or callable(safety_check)) and type(check_interval) in (int,float) and 1 <= check_interval <= 300):
        raise ValueError('invalid object transfer request')
    journal=Path(journal);fd=os.open(journal,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    source=destination=None
    try:
        parent=os.open(journal.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(parent)
        finally:os.close(parent)
        _append(fd,{'phase':'intent','at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    **expected,'operation':operation,'atomic_no_replace_proven':False,
                    'eviction_authorized':False,'retention_authorized':False})
        with tempfile.TemporaryFile() as serr, tempfile.TemporaryFile() as dout, tempfile.TemporaryFile() as derr:
            try:
                source=subprocess.Popen(source_command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=serr,start_new_session=True,preexec_fn=_bound_child_files)
                destination=subprocess.Popen(object_command,stdin=source.stdout,stdout=dout,stderr=derr,start_new_session=True,preexec_fn=_bound_child_files)
                source.stdout.close();deadline=time.monotonic()+timeout;next_check=time.monotonic()+check_interval
                while source.poll() is None or destination.poll() is None:
                    now=time.monotonic()
                    if now>=deadline:raise TimeoutError('object stream deadline exceeded')
                    if safety_check is not None and now>=next_check:
                        safety_check();next_check=time.monotonic()+check_interval
                    time.sleep(min(.05,max(0,deadline-time.monotonic())))
                if source.returncode or destination.returncode:raise ValueError('object stream process failed')
                sent=_json_file(serr,last_line=True)
                if not (sent.get('schema')=='radar-direct-source-stream-v1' and sent.get('relative_path')==expected['relative_path']
                        and sent.get('read_bytes')==expected['size'] and sent.get('sha256')==expected['sha256']
                        and sent.get('source_stable') is True and sent.get('eviction_authorized') is False
                        and sent.get('retention_authorized') is False):raise ValueError('field source receipt differs')
                received=_json_file(dout)
                if not (sent.get('wire_encoding')=='gzip' and type(sent.get('wire_bytes')) is int
                        and sent['wire_bytes']>0 and received.get('schema')=='radar-gzip-object-receiver-v1'
                        and received.get('size')==expected['size'] and received.get('sha256')==expected['sha256']
                        and received.get('wire_bytes')==sent['wire_bytes']
                        and received.get('eviction_authorized') is False and received.get('retention_authorized') is False):
                    raise ValueError('compressed transport receipts differ')
                result={'wire_encoding':'gzip','wire_bytes':sent['wire_bytes'],'phase':'stream-complete',**expected,'object_key':operation['object_key'],
                        'independent_object_readback':False,'atomic_no_replace_proven':False,
                        'immutable_backing_proven':False,'eviction_authorized':False,'retention_authorized':False}
                _append(fd,result);return result
            except BaseException:
                _stop(source);_stop(destination)
                _append(fd,{'phase':'outcome-uncertain',**expected,'object_key':operation['object_key'],
                            'source_exit':source.returncode if source else None,
                            'destination_exit':destination.returncode if destination else None,
                            'eviction_authorized':False,'retention_authorized':False})
                raise
    finally:os.close(fd)
