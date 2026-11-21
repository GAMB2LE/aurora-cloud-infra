"""Sequential fail-closed state machine for a finite provisional archive batch.

An adapter performs the guarded operations. This state machine never retries
an uncertain step and never grants authority to remove any source or local data.
"""
import json
import os
from pathlib import Path
import re

PATH = re.compile(r'Y\d{4}/M\d{2}/D\d{2}/[A-Za-z0-9_.-]+\.(?:LV0|LV0\.NC|LV1|LV1\.NC)\Z')

def run(paths, adapter, journal):
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 20 or len(set(paths)) != len(paths)
            or any(not isinstance(p, str) or not PATH.fullmatch(p) for p in paths)):
        raise ValueError('invalid finite batch')
    journal = Path(journal)
    fd = os.open(journal, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    def event(phase, **extra):
        value={'phase':phase,'eviction_authorized':False,'retention_authorized':False,**extra}
        os.write(fd,(json.dumps(value,sort_keys=True)+'\n').encode());os.fsync(fd)
    complete=0
    try:
        event('batch-start',count=len(paths))
        for path in paths:
            event('item-intent',relative_path=path)
            for step in ('prepare_object', 'admit_object', 'transfer_object', 'reconcile_object',
                         'prepare_gws', 'admit_gws', 'transfer_gws', 'reconcile_gws'):
                event('step-intent',relative_path=path,step=step)
                evidence=getattr(adapter,step)(path)
                if not isinstance(evidence,dict) or evidence.get('accepted') is not True:
                    raise ValueError('step did not return accepted evidence: '+step)
                event('step-complete',relative_path=path,step=step,evidence=evidence)
            event('item-complete',relative_path=path);complete+=1
        event('batch-complete',completed=complete)
        return complete
    except BaseException as exc:
        event('batch-stopped',completed=complete,error_type=type(exc).__name__)
        raise
    finally:
        os.close(fd)
