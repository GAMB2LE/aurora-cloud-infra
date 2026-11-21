#!/usr/bin/env python3
"""Sequentially admit, copy and pair one GWS-first object; never retry a step."""
import json
import os
from pathlib import Path
import subprocess
import sys

from gws_receive import PATH

HERE = Path(__file__).resolve().parent


def run(relative, journal, invoke):
    if not isinstance(relative,str) or PATH.fullmatch(relative) is None:
        raise ValueError('invalid radar path')
    journal = Path(journal)
    fd = os.open(journal,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    def event(phase, **extra):
        raw = (json.dumps({'phase':phase,'relative_path':relative,'eviction_authorized':False,
                           'retention_authorized':False,**extra},sort_keys=True)+'\n').encode()
        while raw:
            n=os.write(fd,raw)
            if n<=0:raise OSError('short journal write')
            raw=raw[n:]
        os.fsync(fd)
    try:
        parent=os.open(journal.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(parent)
        finally:os.close(parent)
        event('step-intent',step='admission')
        admitted=invoke('admission',relative)
        if not (isinstance(admitted,dict) and admitted.get('expected',{}).get('relative_path')==relative
                and admitted.get('transfer_started') is False and admitted.get('object_verified') is False
                and admitted.get('eviction_authorized') is False and admitted.get('retention_authorized') is False
                and isinstance(admitted.get('preparation'),str)):
            raise ValueError('admission evidence differs')
        event('step-complete',step='admission',evidence=admitted)
        event('step-intent',step='object')
        copied=invoke('object',admitted['preparation'])
        if not (isinstance(copied,dict) and copied.get('state')=='object-recorded-and-replicated'
                and copied.get('relative_path')==relative and copied.get('gws_pair_recorded') is False
                and copied.get('immutable_backing_proven') is False and copied.get('eviction_authorized') is False
                and copied.get('retention_authorized') is False):
            raise ValueError('object reconciliation evidence differs')
        event('step-complete',step='object',evidence=copied)
        event('step-intent',step='pair')
        paired=invoke('pair',relative)
        if not (isinstance(paired,dict) and paired.get('state')=='provisional-pair-replicated'
                and paired.get('relative_path')==relative
                and paired.get('immutable_backing_proven') is False and paired.get('eviction_authorized') is False
                and paired.get('retention_authorized') is False):
            raise ValueError('pair reconciliation evidence differs')
        event('step-complete',step='pair',evidence=paired)
        event('complete')
        return paired
    except BaseException as exc:
        event('stopped',error_type=type(exc).__name__)
        raise
    finally:os.close(fd)


def invoke(step, argument, *, journal=None):
    scripts={'admission':'prepare_pending_object.py','object':'apply_pending_object.py','pair':'reconcile_gws_first_pair.py'}
    limits={'admission':1800,'object':25200,'pair':2400}
    result=subprocess.run([sys.executable,str(HERE/scripts[step]),argument],capture_output=True,timeout=limits[step])
    if result.returncode:
        if journal is not None:
            diagnostic = Path(str(journal) + '.' + step + '.stderr')
            fd = os.open(diagnostic, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(result.stderr[-65536:])
                stream.flush()
                os.fsync(stream.fileno())
            parent = os.open(diagnostic.parent, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try: os.fsync(parent)
            finally: os.close(parent)
        raise RuntimeError(step+' stopped; inspect exact operation before retry')
    lines=result.stdout.decode().splitlines()
    if not lines:raise ValueError(step+' produced no result')
    return json.loads(lines[-1])


if __name__=='__main__':
    journal = Path(sys.argv[2])
    print(json.dumps(run(sys.argv[1],journal,lambda step,arg: invoke(step,arg,journal=journal)),sort_keys=True))
