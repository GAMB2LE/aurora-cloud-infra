#!/usr/bin/env python3
"""Run a finite direct object/GWS batch using guarded, per-file workers.

Stops on the first uncertain result. It never removes data or changes holds.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time

import bounded_backlog_sequence as sequence
import prepare_direct_candidate as direct
import prepare_gws_candidate as gws

HERE=Path(__file__).resolve().parent
ROOT=direct.ROOT
OBJ_TEMPLATE=HERE/'reconcile_provisional_object_gzip_2155.py'
GWS_TEMPLATE=HERE/'reconcile_object_gws_gzip_1430.py'

def require_reconciled(base):
    if any(os.path.lexists(base/name) for name in ('bounded-backlog-unresolved.json', 'gws-first-pair-unresolved.json', 'gws-pending-object-unresolved.json')):
        raise RuntimeError('unresolved transfer requires reconciliation before another batch')

def command(args, timeout=60):
    return subprocess.run(direct.gateway(args),capture_output=True,check=True,timeout=timeout).stdout

def state(unit):
    raw=command(['systemctl','show',unit,'-p','LoadState','-p','ActiveState','-p','SubState',
                 '-p','Result','-p','ExecMainStatus','-p','ExecMainPID','-p','InvocationID']).decode()
    return dict(line.split('=',1) for line in raw.splitlines() if '=' in line)

def successful(properties):
    return (properties.get('LoadState')!='not-found' and properties.get('ActiveState')=='active'
            and properties.get('SubState')=='exited' and properties.get('Result')=='success'
            and properties.get('ExecMainStatus')=='0')

def wait_unit(unit, timeout):
    deadline=time.monotonic()+timeout
    while True:
        try:
            properties=state(unit)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            # An observation failure says nothing about the remote worker.
            # Only retry this read-only query; never repeat a launch here.
            if time.monotonic()>=deadline:raise TimeoutError('unit observation unresolved: '+unit)
            time.sleep(3)
            continue
        if successful(properties):return properties
        if properties.get('LoadState')=='not-found' or properties.get('ActiveState') in ('failed','inactive'):
            raise RuntimeError('unit failed or disappeared: '+unit)
        if time.monotonic()>=deadline:raise TimeoutError('unit still unresolved: '+unit)
        time.sleep(3)

class AdmissionCapacityUnavailable(RuntimeError):
    """A terminal read-only admission hit the existing capacity threshold."""


def capacity_refusal(unit, failed):
    invocation = failed.get('InvocationID', '')
    if (failed.get('SubState') == 'failed' and failed.get('Result') == 'exit-code'
            and failed.get('ExecMainStatus') == '1' and re.fullmatch('[0-9a-f]{32}', invocation)):
        logs = command(['sudo','-n','journalctl','-u',unit,'_SYSTEMD_INVOCATION_ID='+invocation,
                        '-o','cat','--no-pager'], timeout=60)
        last = logs.rstrip().splitlines()[-1:]
        if last and re.fullmatch(rb'ValueError: direct-transfer capacity insufficient: /(?:data|project)?', last[0]):
            return last[0].decode().removeprefix('ValueError: ')
    return None


def admission(unit, request, *, object_destination=False):
    try:
        props=wait_unit(unit,1200)
    except RuntimeError:
        blocker = capacity_refusal(unit, state(unit))
        if blocker:
            raise AdmissionCapacityUnavailable(blocker) from None
        raise
    if not re.fullmatch('[0-9a-f]{32}',props.get('InvocationID','')):raise ValueError('admission identity missing')
    lines=command(['sudo','-n','journalctl','-u',unit,'_SYSTEMD_INVOCATION_ID='+props['InvocationID'],
                   '-o','cat','--no-pager'],timeout=60).decode(errors='replace').splitlines()
    expected=request['expected_source'];matches=[]
    for line in lines:
        try: row=json.loads(line)
        except json.JSONDecodeError:continue
        if isinstance(row,dict) and row.get('mode')=='read-only':matches.append(row)
    if len(matches)!=1:raise ValueError('admission evidence missing or ambiguous')
    value=matches[0];target=value.get('expected',{})
    if not (value.get('transfer_started') is False and value.get('retention_authorized') is False
            and value.get('eviction_authorized') is False and target.get('relative_path')==request['relative_path']
            and target.get('sha256')==expected['sha256'] and target.get('size')==expected['identity'][2]
            and target.get('mtime_ns')==expected['identity'][3]
            and (not object_destination or (value.get('candidate_absent') is True and value.get('object_head_control') is True))):
        raise ValueError('admission evidence differs')
    return {'accepted':True,'invocation_id':props['InvocationID']}

def launch(unit, out):
    args=json.loads((out/'launcher-args.json').read_bytes())
    command(['sudo','-n','systemd-run','--unit='+unit.removesuffix('.service'),
             '--property=Type=exec','--property=RemainAfterExit=yes','--property=RuntimeMaxSec=6h15min',
             '--nice=15',*args,'--apply'])
    props=state(unit)
    pid=props.get('ExecMainPID','');invocation=props.get('InvocationID','')
    if not (pid.isdigit() and int(pid)>0 and re.fullmatch('[0-9a-f]{32}',invocation)):
        raise RuntimeError('worker launch identity unresolved')
    return {'unit':unit,'invocation_id':invocation,'main_pid':int(pid)}

def reconcile(script, *args):
    env=dict(os.environ);env['PYTHONPATH']=str(HERE)
    process=subprocess.run([sys.executable,str(script),*args],capture_output=True,timeout=1200,env=env)
    if process.returncode:
        diagnostic = Path(str(script)+'.stderr')
        with diagnostic.open('xb') as stream:
            stream.write(process.stderr[-65536:])
            stream.flush()
            os.fsync(stream.fileno())
        raise RuntimeError(f'reconciliation failed or uncertain; see {diagnostic}')
    value=json.loads(process.stdout)
    if value.get('eviction_authorized') is not False or value.get('retention_authorized') is not False:
        raise ValueError('reconciliation authorization differs')
    return value

class Driver:
    def prepare_object(self,path):
        self.obj=direct.prepare(path);self.obj_out=Path(self.obj['out']);self.request=json.loads((self.obj_out/'request.json').read_bytes())
        return {'accepted':True,'output':str(self.obj_out),'unit':self.obj['admission_unit']}
    def admit_object(self,path):return admission(self.obj['admission_unit'],self.request,object_destination=True)
    def transfer_object(self,path):
        unit='aurora-object-direct-'+self.obj['operation']+'.service';identity=launch(unit,self.obj_out)
        (self.obj_out/'service-identity.json').write_text(json.dumps(identity,indent=2)+'\n')
        props=wait_unit(unit,22500)
        if props.get('InvocationID')!=identity['invocation_id'] or props.get('ExecMainPID')!=str(identity['main_pid']):raise ValueError('object worker identity changed')
        return {'accepted':True,**identity}
    def reconcile_object(self,path,*,resume_import=False):
        name=self.obj_out.name.removeprefix('direct-object-gzip-preparation-');op=self.obj['operation'];pin=hashlib.sha256((self.obj_out/'request.json').read_bytes()).hexdigest()
        raw=OBJ_TEMPLATE.read_text().replace('direct-object-gzip-preparation-20261106T2155Z',self.obj_out.name).replace('20261027-gzip-next-2155z-0',op).replace('b3074eefa60990fd3397f4ca96c0c11da43812a168f5477b0e6dbd8e15d9ef10',pin)
        script=self.obj_out/('reconcile-'+name+('-resume-import' if resume_import else '')+'.py')
        with script.open('x') as f:f.write(raw)
        result=reconcile(script,'--resume-import') if resume_import else reconcile(script)
        if result.get('object_verified') is not True:raise ValueError('object not verified')
        return {'accepted':True,'snapshot_sha256':result['snapshot_sha256']}
    def prepare_gws(self,path):
        name=self.obj_out.name.removeprefix('direct-object-gzip-preparation-');self.gws=gws.prepare(name);self.gws_out=Path(self.gws['out']);self.gws_request=json.loads((self.gws_out/'request.json').read_bytes())
        return {'accepted':True,'output':str(self.gws_out),'unit':self.gws['admission_unit']}
    def admit_gws(self,path):return admission(self.gws['admission_unit'],self.gws_request)
    def transfer_gws(self,path):
        unit='aurora-direct-'+self.gws['operation']+'.service';identity=launch(unit,self.gws_out)
        (self.gws_out/'transfer-service-identity.json').write_text(json.dumps(identity,indent=2)+'\n')
        props=wait_unit(unit,22500)
        if props.get('InvocationID')!=identity['invocation_id'] or props.get('ExecMainPID')!=str(identity['main_pid']):raise ValueError('GWS worker identity changed')
        return {'accepted':True,**identity}
    def reconcile_gws(self,path):
        raw=GWS_TEMPLATE.read_text().replace('object-gws-gzip-preparation-20261106T1430Z',self.gws_out.name).replace('20261027-gws-gzip-next-1430z-0',self.gws['operation']).replace('direct-object-gzip-preparation-20261106T1412Z',self.obj_out.name)
        script=self.gws_out/'reconcile-generated.py'
        with script.open('x') as f:f.write(raw)
        result=reconcile(script)
        if result.get('gws_verified') is not True:raise ValueError('GWS not verified')
        return {'accepted':True,'snapshot_sha256':result['snapshot_sha256']}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--queue',type=Path,required=True);parser.add_argument('--count',type=int,required=True);parser.add_argument('--journal',type=Path,required=True);args=parser.parse_args()
    if not 1<=args.count<=20:raise ValueError('invalid count')
    value=json.loads(args.queue.read_bytes())
    database=sqlite3.connect('file:'+str(direct.BASE/'working-ledger.sqlite')+'?mode=ro',uri=True)
    try:
        database.execute('BEGIN')
        known={row[0] for table in ('provisional_object_versions','provisional_gws_versions')
               for row in database.execute('SELECT DISTINCT relative_path FROM '+table)}
    finally:database.close()
    paths=[x['relative_path'] for x in value['candidates'] if x['relative_path'] not in known][:args.count]
    if len(paths)!=args.count:raise ValueError('not enough unrecorded candidates')
    lock=direct.BASE/'bounded-backlog-coordinator.lock';fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        require_reconciled(direct.BASE)
        print(json.dumps({'completed':sequence.run(paths,Driver(),args.journal),'retention_authorized':False,'eviction_authorized':False}))
    finally:os.close(fd)

if __name__=='__main__':main()
