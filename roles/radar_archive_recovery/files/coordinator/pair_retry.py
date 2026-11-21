"""Record pair failures and allow only bounded, proved pre-intent retries.

No science transfers, local eviction or retention authority are granted.
"""
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import stat
import sys
import time
import uuid

from bounded_backlog_runner import require_reconciled
from gws_pending_replay import classify_pending
from per_file_queue import Queue, writer_lock
from prepare_direct_candidate import BASE
from reconcile_gws_first_pair import check_retention_and_fence
from verify_pending_gws_live import selected_endpoint

HERE=Path(__file__).resolve().parent
STATE=BASE/'per-file-archive-queue.sqlite'
PENDING=BASE/'gws-only-pending.sqlite'
LEDGER=BASE/'working-ledger.sqlite'
PAIR=HERE/'reconcile_gws_first_pair.py'
TIMEOUT=2400

class PairVerificationUnavailable(RuntimeError):
    """A specifically identified, pre-intent GWS transport failure."""


def pair_directories():
    return sorted(p.name for p in BASE.glob('gws-first-pair-reconcile-*'))


def no_pair_process():
    proc=Path('/proc')
    if not proc.is_dir():raise RuntimeError('cannot independently inspect pair processes')
    for p in proc.iterdir():
        if not p.name.isdigit() or int(p.name)==os.getpid():continue
        try: fields=(p/'cmdline').read_bytes().split(b'\0')
        except FileNotFoundError:continue
        except (PermissionError,OSError) as error:raise RuntimeError('pair process inspection unavailable') from error
        if any(Path(os.fsdecode(x)).name=='reconcile_gws_first_pair.py' for x in fields if x):
            raise RuntimeError('pair process already running')


def validate(op):
    with contextlib.closing(Queue(STATE,readonly=True)) as queue:
        if queue.outstanding()!=op:raise ValueError('outstanding operation changed')
    if op.get('target')!='pair-recorded' or not re.fullmatch('[0-9a-f]{32}',op.get('id','')):
        raise ValueError('not an exact pair operation')
    require_reconciled(BASE)
    check_retention_and_fence()
    matches=[x for x in classify_pending(PENDING,LEDGER) if x['expected']==op['expected']]
    if len(matches)!=1 or matches[0]['status']!='pair-reconciliation-needed':
        raise ValueError('pair ledger status requires observation or reconciliation')
    no_pair_process()


def transient(stderr):
    # Only our typed, bounded diagnostic identifies the read-only GWS phase.
    lines=[x.removeprefix('PAIR_GWS_READBACK_FAILURE ') for x in stderr.splitlines()
           if x.startswith('PAIR_GWS_READBACK_FAILURE ')]
    if len(lines)!=1:return False
    try: value=json.loads(lines[0])
    except (ValueError,TypeError):return False
    if not (isinstance(value,dict) and value.get('phase')=='gws-readback'
            and value.get('returncode')==255 and isinstance(value.get('stderr'),str)
            and value.get('endpoint')==selected_endpoint()):
        return False
    forbidden=('permission denied','host key verification failed','authentication','hash differs','checksum')
    s=value['stderr'].lower()
    banner = selected_endpoint() == 'rrniii@xfer-vm-01.jasmin.ac.uk' and s.strip().splitlines() == ['connection timed out during banner exchange',
                                      'connection to 130.246.129.38 port 22 timed out']
    return not any(x in s for x in forbidden) and (banner or
        any(s.strip() == f'ssh: connect to host {selected_endpoint().split("@",1)[1]} port 22: {cause}'
            for cause in ('connection timed out','network is unreachable')))


def run_once(op):
    validate(op)
    before=pair_directories(); started=time.time()
    try:
        p=subprocess.run([sys.executable,str(PAIR),op['expected']['relative_path']],capture_output=True,timeout=TIMEOUT)
        code=p.returncode; stderr=p.stderr[-65536:]; stdout=p.stdout[-65536:]; timed_out=False
    except subprocess.TimeoutExpired as e:
        code=None;stderr=(e.stderr or b'')[-65536:];stdout=(e.stdout or b'')[-65536:];timed_out=True
    except OSError as e:
        code=None;stderr=('pair process could not start: '+str(e)).encode()[-65536:];stdout=b'';timed_out=False
    after=pair_directories()
    record={'schema':'radar-pair-attempt-v1','operation':op,'started':started,'finished':time.time(),
            'returncode':code,'timed_out':timed_out,'stderr':stderr.decode(errors='replace'),
            'stdout':stdout.decode(errors='replace'),'directories_before':before,'directories_after':after,
            'eviction_authorized':False,'retention_authorized':False}
    path=BASE/('pair-attempt-'+op['id']+'-'+uuid.uuid4().hex+'.json')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(record,f,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
    d=os.open(BASE,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(d)
    finally:os.close(d)
    if timed_out or code!=0:
        if not timed_out and before==after and transient(record['stderr']):
            validate(op)
            raise PairVerificationUnavailable('pair-gws-transient')
        raise RuntimeError('pair attempt failed; see '+str(path))
    return json.loads(stdout.decode().splitlines()[-1])


def retry_under_lock(operation_id):
    """Retry with caller holding the per-file coordinator lock."""
    if not re.fullmatch('[0-9a-f]{32}',operation_id):raise ValueError('invalid operation id')
    with contextlib.closing(Queue(STATE,readonly=True)) as q:op=q.outstanding()
    if not op or op['id']!=operation_id:raise ValueError('different outstanding pair')
    paths=list(BASE.glob('pair-attempt-'+operation_id+'-*.json'))
    if not 1<=len(paths)<=3:raise ValueError('retry limit or diagnostics unavailable')
    records=[]
    for path in paths:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as file:
            info=os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>150000:
                raise ValueError('invalid pair attempt diagnostic')
            records.append(json.load(file))
    records.sort(key=lambda r:r['finished'])
    for rec in records:
        if (rec.get('schema')!='radar-pair-attempt-v1' or rec.get('operation')!=op
                or rec.get('returncode') in (None,0) or rec.get('timed_out') is not False
                or rec.get('directories_before')!=rec.get('directories_after')
                or not transient(rec.get('stderr',''))):
            raise ValueError('pair retry not proven pre-intent transient')
    if time.time()-records[-1]['finished']<300:raise ValueError('pair retry backoff not elapsed')
    if pair_directories()!=records[-1]['directories_after']:
        raise ValueError('pair reconciliation directories changed')
    validate(op)
    return run_once(op)


def retry(operation_id):
    """Operator wrapper; coordinator invokes retry_under_lock directly."""
    with writer_lock(BASE/'per-file-coordinator.lock'):
        return retry_under_lock(operation_id)
