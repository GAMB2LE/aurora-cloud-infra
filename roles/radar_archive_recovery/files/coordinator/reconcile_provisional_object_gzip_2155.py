#!/usr/bin/env python3
"""Reconcile a completed provisional object copy and replicate its ledger.

Never start or retry a science transfer. This script requires a terminal
successful service, validates the exact journal, source request, pinned writer
and independent object receipt, and treats any uncertain snapshot publication
as a separate reconciliation task. It gives no retention or eviction authority.
"""
import datetime,hashlib,importlib.util,json,os,shlex,subprocess,sys
from pathlib import Path
import provisional_object_evidence as provisional
from prepare_direct_candidate import BASE, INFRA, ROOT, GWS_OBJECT_LAUNCHER, GWS_OBJECT_PINS, gateway, run_remote
from gws_object_release import resolve as resolve_object_release, verify_pins, authenticate_worker, OLD, NEW

OUT=BASE/'direct-object-gzip-preparation-20261106T2155Z'
SERVICE='aurora-object-direct-20261027-gzip-next-2155z-0.service'
REMOTE='/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/direct-object-v1'
OP='20261027-gzip-next-2155z-0'
WRITER_SHA='732617202df9b82d3a84fac413e3bc7e752206b39bcbb78019ac290cfb99e791'


def sha(raw):return hashlib.sha256(raw).hexdigest()
def remote(command,timeout=60):
    result=subprocess.run(gateway(command),capture_output=True,timeout=timeout)
    if result.returncode:raise ValueError('remote evidence unavailable')
    return result.stdout

def save(path,raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())

def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def main(*, resume_import=False):
    state=remote(['systemctl','show',SERVICE,'-p','ActiveState','-p','SubState','-p','Result','-p','ExecMainStatus','-p','LoadState','-p','InvocationID','-p','ExecMainPID']).decode()
    properties=dict(x.split('=',1) for x in state.splitlines() if '=' in x)
    identity=json.loads((OUT/'service-identity.json').read_bytes())
    recovery_path = OUT/'landed-object-readback.json'
    recovered = recovery_path.is_file()
    if identity['unit']!=SERVICE:
        raise ValueError('service has not completed successfully; do not import or retry')
    if recovered:
        recovery = json.loads(recovery_path.read_bytes())
        group = remote(['systemctl','show',SERVICE,'-p','ControlGroup','--value']).strip()
        if (group or properties.get('SubState') != 'failed' or properties.get('Result') != 'exit-code'
                or properties.get('InvocationID') != identity['invocation_id']
                or properties.get('ExecMainPID') != str(identity['main_pid'])
                or any(properties.get(k) != v for k, v in recovery['worker'].items())):
            raise ValueError('landed object worker identity differs')
    elif properties.get('ActiveState')!='active' or properties.get('SubState')!='exited':
        raise ValueError('service has not completed successfully; do not import or retry')
    elif properties.get('LoadState')=='not-found':
        manager_raw=remote(['sudo','-n','journalctl','-u',SERVICE,'_PID=1','--no-pager','-o','json'])
        records=[json.loads(line) for line in manager_raw.splitlines()]
        records=[r for r in records if r.get('INVOCATION_ID')==identity['invocation_id'] and r.get('UNIT')==SERVICE]
        successes=[r for r in records if r.get('MESSAGE_ID')=='7ad2d189f7e94e70a38c781354912448' and r.get('MESSAGE')==SERVICE+': Deactivated successfully.']
        if len(successes)!=1 or any('failed' in str(r.get('MESSAGE','')).lower() for r in records):raise ValueError('transient unit lacks successful termination')
    elif not (properties.get('Result')=='success' and properties.get('ExecMainStatus')=='0'
              and properties.get('InvocationID')==identity['invocation_id'] and properties.get('ExecMainPID')==str(identity['main_pid'])):
        raise ValueError('service has not completed successfully; do not import or retry')
    if remote(['sudo','-n','sha256sum',REMOTE.replace('direct-object-v1','direct-object-gzip-supervisor-v2')+'/direct_object_gzip_recovery.py']).decode().split()[:1]!=[WRITER_SHA]:raise ValueError('remote writer differs')
    versioned=REMOTE.replace('direct-object-v1','direct-object-gzip-supervisor-v2')
    for name,pin in [('direct_object_gzip_launcher.py','22d6ef92b95eccdd8c39621ea4c0123921294a8b40a32147fa826f98e332ab67'),('direct_object_gzip_pipeline.py','07e076bbee03d8d9532e56a6d8eb75aeadea2d30e57bed4298c011f04277b3a4')]:
        if remote(['sudo','-n','sha256sum',versioned+'/'+name]).decode().split()[:1]!=[pin]:raise ValueError('staged component differs: '+name)
    release, args = resolve_object_release(OUT)
    if args[1] in (OLD, NEW):
        authenticate_worker(SERVICE,args,run_remote)
        verify_pins(release,run_remote)
        if args[1] == OLD and not release['pins']:
            for component, pin in GWS_OBJECT_PINS.items():
                if remote(['sudo','-n','sha256sum',component]).decode().split()[:1] != [pin]:
                    raise ValueError('GWS object component differs: '+component)
    writer=(ROOT/'tmp/radar-direct-archive-20261105/direct_object_gzip_recovery.py').read_bytes()
    if sha(writer)!=WRITER_SHA:raise ValueError('local writer differs')
    request=(OUT/'request.json').read_bytes()
    if sha(request)!='b3074eefa60990fd3397f4ca96c0c11da43812a168f5477b0e6dbd8e15d9ef10':raise ValueError('request differs')
    prefix=REMOTE+'/transfer-'+OP+'.jsonl'
    journal_raw=remote(['sudo','-n','cat',prefix])
    preflight_raw=remote(['sudo','-n','cat',prefix+'.preflight.json'])
    if recovered:
        guard_raw = (BASE/'gws-pending-object-unresolved.json').read_bytes()
        if (sha(journal_raw) != recovery['journal_sha256'] or sha(preflight_raw) != recovery['preflight_sha256']
                or sha(guard_raw) != recovery['guard_sha256']):
            raise ValueError('landed object recovery evidence differs')
        preflight = json.loads(preflight_raw)
        stream = json.loads(journal_raw.splitlines()[-1])
        readback = recovery['readback']
        receipt = {'schema':'radar-direct-object-version-v1', 'expected':preflight['expected'],
                   'source_identity':preflight['source']['identity'][:9],
                   'object_key':preflight['operation']['object_key'], 'object_metadata':readback['metadata_before'],
                   'independent_readback':readback['readback'], 'stream':stream, 'landed_recovery':recovery,
                   'gws_verified':False, 'atomic_no_replace_proven':False, 'immutable_backing_proven':False,
                   'eviction_authorized':False, 'retention_authorized':False}
        proof_raw = (json.dumps(receipt, sort_keys=True)+'\n').encode()
    else:
        proof_raw=remote(['sudo','-n','cat',prefix+'.readback.json'])
    proof=json.loads(proof_raw);preflight=json.loads(preflight_raw);events=[json.loads(line) for line in journal_raw.splitlines()]
    if ([event.get('phase') for event in events]!=['intent','outcome-uncertain' if recovered else 'stream-complete'] or proof.get('stream')!=events[1]
        or preflight.get('operation')!=events[0].get('operation') or preflight.get('expected')!=proof.get('expected')
        or preflight.get('operation',{}).get('object_key')!=proof.get('object_key')):
        raise ValueError('service evidence differs')
    run=OUT/'ledger-reconcile'
    journal=run/'reconcile.jsonl'
    files={'object-readback.json':proof_raw,'transfer-journal.jsonl':journal_raw,'preflight.json':preflight_raw}
    if resume_import:
        events=[json.loads(line) for line in journal.read_bytes().splitlines()]
        if ([entry.get('phase') for entry in events] != ['intent','provisional-evidence-imported']
                or events[0].get('receipt_sha256') != sha(proof_raw)
                or events[0].get('request_sha256') != sha(request)
                or events[1].get('object_key') != proof.get('object_key')
                or any(entry.get(flag) is not False for entry in events
                       for flag in ('retention_authorized','eviction_authorized'))
                or any((run/name).read_bytes() != content for name,content in files.items())):
            raise ValueError('existing ledger reconciliation cannot resume before snapshot')
        flags=os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW
    else:
        run.mkdir(mode=0o700,exist_ok=False)
        for name,content in files.items():
            save(run/name,content)
        flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW
    fd=os.open(journal,flags,0o600)
    def event(phase,**extra):
        raw=(json.dumps({'phase':phase,'eviction_authorized':False,'retention_authorized':False,**extra},sort_keys=True)+'\n').encode()
        os.write(fd,raw);os.fsync(fd)
    try:
        if not resume_import:
            event('intent',receipt_sha256=sha(proof_raw),request_sha256=sha(request))
        ledger=load_module('provisional_ledger',INFRA/'roles/radar_archive_cache/files/aurora-radar-archive-ledger.py')
        db=ledger.connect(BASE/'working-ledger.sqlite')
        try:
            stamp=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
            if resume_import:
                rows=db.execute('SELECT recorded_at FROM provisional_object_versions WHERE receipt_sha256=?',
                                (sha(proof_raw),)).fetchall()
                if len(rows)!=1:
                    raise ValueError('exact prior object import missing')
                stamp=rows[0][0]
            result=provisional.import_receipt(db,proof_raw,sha(proof_raw),request,sha(request),writer,WRITER_SHA,stamp)
            event('prior-import-revalidated' if resume_import else 'provisional-evidence-imported',
                  object_key=result['object_key'])
            snap=ledger.snapshot_database(db,BASE/'ledger-snapshots')
        finally:db.close()
        path=Path(snap['path']);digest=snap['snapshot_sha256'];event('snapshot-created',sha256=digest)
        commands=[('primary',[sys.executable,str(INFRA/'scripts/install_radar_snapshot_primary.py'),str(path),'--sha256',digest]),
                  ('archives',[sys.executable,str(INFRA/'scripts/publish_radar_ledger_snapshot.py'),str(path),'--sha256',digest,'--replicate-object-unprotected'])]
        for name,cmd in commands:
            event(name+'-intent',sha256=digest);p=subprocess.run(cmd,capture_output=True,timeout=360)
            save(run/(name+'-stdout.json'),p.stdout);save(run/(name+'-stderr.txt'),p.stderr)
            if p.returncode:raise RuntimeError(name+' publication uncertain; reconcile before retry')
            event(name+'-complete',sha256=digest)
        published=json.loads((run/'archives-stdout.json').read_bytes());object_path=published['stores']['object']['path']
        cmd=[sys.executable,str(INFRA/'scripts/verify_radar_snapshot_replica.py'),str(path),'--sha256',digest,'--replica',object_path]
        p=subprocess.run(cmd,capture_output=True,timeout=360);save(run/'independent-verification.json',p.stdout);save(run/'independent-verification-stderr.txt',p.stderr)
        if p.returncode:raise RuntimeError('snapshot independent verification failed')
        event('complete',sha256=digest);print(json.dumps({'snapshot_sha256':digest,'object_key':result['object_key'],
           'object_verified':True,'gws_verified':False,'immutable_backing_proven':False,'eviction_authorized':False,'retention_authorized':False}))
    finally:os.close(fd)

if __name__=='__main__':
    if sys.argv[1:] not in ([], ['--resume-import']):
        raise SystemExit('unexpected reconciliation argument')
    main(resume_import=bool(sys.argv[1:]))
