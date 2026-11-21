#!/usr/bin/env python3
"""Resume the exact terminal staged transfer recorded by the cloud coordinator."""
import hashlib
import datetime as dt
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import time
import tempfile
from gws_worker_release import resolve_preparation, authenticate_worker, verify_release_pins

from bounded_backlog_runner import state, successful, wait_unit
from prepare_direct_candidate import run_remote

ROOT = '/var/lib/aurora-radar-coordinator-v1'
BASE = ROOT+'/output/aurora-archive-repair-20261026/archive-first-implementation-20261027'
REMOTE = '/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/direct-staged-v1'
HERE = Path(__file__).resolve().parent
COORD = 'aurora-radar-coordinator.service'


class GwsCapacityUnavailable(RuntimeError):
    """The staged file remains held behind the existing capacity gate."""


class IncompleteGwsStage(RuntimeError):
    """The failed worker recorded an unpublished partial outcome."""


def check_capacity(size):
    code = f"import sys;sys.path.insert(0,{REMOTE!r});from direct_capacity import check;check(int(sys.argv[1]))"
    try:
        run_remote(['sudo', '-n', 'env', 'PYTHONDONTWRITEBYTECODE=1', 'python3', '-c',
                    code, str(size)], timeout=30)
    except subprocess.CalledProcessError as error:
        if re.search(rb'ValueError: direct-transfer capacity insufficient: /(?:data|project)?\s*$', error.stderr):
            raise GwsCapacityUnavailable('GWS publication capacity below threshold') from None
        raise


def save_recovery_record(path, value):
    raw = (json.dumps(value, sort_keys=True) + '\n').encode()
    run_remote(['env','RADAR_COORDINATOR_ROOT='+ROOT,'RADAR_COORDINATOR_LOCAL=1',
                'PYTHONPATH='+str(HERE),'PYTHONDONTWRITEBYTECODE=1',
                'python3','-c','import sys; from pathlib import Path; from capture_gws_only_refresh import save; '
                'save(Path(sys.argv[1]), sys.stdin.buffer.read())', path], data=raw)


def fetch(path, root=False):
    return run_remote((['sudo','-n'] if root else [])+['cat',str(path)]).stdout


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def recovery_inputs(bundle, expected):
    """Reuse an installed bundle or obtain fresh independently pinned proof.

    Never infer missing receipts from an I/O failure and never modify the
    original worker's evidence. The installed bundle is validated later.
    """
    present = run_remote(['sudo','-n','python3','-c',
        'import os,sys; print(int(os.path.lexists(sys.argv[1])))',bundle], timeout=30).stdout.strip()
    if present not in (b'0',b'1'):
        raise ValueError('invalid recovery bundle existence result')
    if present == b'1':
        return fetch(bundle+'/continuity.json',True), fetch(bundle+'/observation.json',True)
    verifier = REMOTE+'/verify_released_radar_continuity.py'
    verifier_sha = 'a956e6c556dbf9a57c85983d1878c92d4bbd8cd3b7508957245f18f2caea44d7'
    acceptance = fetch(REMOTE+'/released-radar-acceptance.json',True)
    acceptance_sha = 'f0edc78e2d99775dff9eff914f8eadd053e98b57f4e8346b8add8953d651f238'
    if sha(acceptance) != acceptance_sha or sha(fetch(verifier,True)) != verifier_sha:
        raise ValueError('fresh continuity controls differ from reviewed pins')
    continuity = run_remote(['sudo','-n','python3',verifier,'--acceptance-sha256',acceptance_sha],
                            data=acceptance,timeout=1800).stdout
    from capture_gws_only_refresh import validate_refreshed
    proof = validate_refreshed(continuity, hashlib.sha256(continuity).hexdigest())
    checked = dt.datetime.fromisoformat(proof['checked_at'].replace('Z','+00:00'))
    if checked.tzinfo is None or not 0 <= (dt.datetime.now(dt.timezone.utc)-checked).total_seconds() <= 600:
        raise ValueError('fresh continuity proof has invalid age')
    observation = {'relative_path':expected['relative_path'],'size':expected['size'],
                   'mtime_seconds':expected['mtime_ns']//10**9,'observed_at':time.time()}
    return continuity, json.dumps(observation,sort_keys=True).encode()


def pinned_template(name, replacements):
    raw = (HERE/name).read_text()
    for original, replacement in replacements.items():
        if original not in raw:
            raise ValueError('recovery template differs: '+original)
        raw = raw.replace(original,replacement)
    return raw


def verify_resume_template(release):
    if release['id'] in {'gzip-v4-vm01','gzip-v5-vm01-lanes'}:
        name = release['resume_template']
        if name != ('resume_stage_launcher_vm01_lanes.py' if release['id'] == 'gzip-v5-vm01-lanes' else 'resume_stage_launcher_vm01.py'):
            raise ValueError('unexpected vm01 resume template')
        if sha((HERE/name).read_bytes()) != release.get('resume_template_sha256'):
            raise ValueError('resume template pin differs')


def recorded_worker(name, arguments):
    """Read the reserved unit and authenticate its command before adopting it."""
    unit = name + '.service'
    props = state(unit)
    if props['LoadState'] == 'not-found':
        return None
    if not (re.fullmatch(r'[0-9a-f]{32}', props.get('InvocationID', ''))
            and props.get('ExecMainPID', '').isdigit() and int(props['ExecMainPID']) > 0):
        raise ValueError('recovery worker identity missing')
    command = run_remote(['systemctl', 'show', unit, '-p', 'ExecStart', '--value']).stdout.decode()
    if 'argv[]=' + shlex.join(arguments) + ' ;' not in command:
        raise ValueError('recovery worker command differs from reserved launch')
    return props


def observe_worker(name, arguments, timeout):
    before = recorded_worker(name, arguments)
    if before is None:
        raise ValueError('reserved recovery worker disappeared')
    after = wait_unit(name + '.service', timeout)
    if any(after[key] != before[key] for key in ('InvocationID', 'ExecMainPID')):
        raise ValueError('recovery worker identity changed')
    return after


def run():
    if state(COORD)['SubState'] != 'failed':
        raise ValueError('recovery requires the failed coordinator')
    status = json.loads(fetch(BASE+'/per-file-status.json'))
    recover(status['outstanding'], coordinator_owned=False)


def recover(pending, *, coordinator_owned=True):
    if not pending or pending['target'] != 'gws-verified':
        raise ValueError('no recorded GWS publication to recover')
    expected = pending['expected']
    prep = pending['intent']['preparation']
    stamp = Path(prep['out']).name.removeprefix('direct-gws-only-preparation-')
    if prep['out'] != BASE+'/direct-gws-only-preparation-'+stamp or not re.fullmatch(r'20\d{6}T\d{6}Z',stamp):
        raise ValueError('unexpected preparation path')
    operation = '20261027-gws-only-'+stamp.lower()+'-0'
    resume = '20261027-gws-resume-'+stamp.lower()+'-0'
    unit = 'aurora-direct-'+operation+'.service'
    identity = json.loads(fetch(prep['out']+'/service-identity.json'))
    # Resolve from the durable preparation, not the currently selected default.
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp)
        for name in ('launcher-args.json', 'service-identity.json'):
            (local/name).write_bytes(fetch(prep['out']+'/'+name))
        record = run_remote(['python3','-c',
            'import os,sys\ntry: fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW)\nexcept FileNotFoundError: sys.exit(0)\nwith os.fdopen(fd,"rb") as f: sys.stdout.buffer.write(f.read())',
            prep['out']+'/worker-release.json']).stdout
        if record:
            (local/'worker-release.json').write_bytes(record)
        release = resolve_preparation(local, intent_preparation=prep)
    authenticate_worker(unit,release['launcher_args'],run_remote)
    verify_release_pins(release,run_remote)
    p = state(unit)
    if (prep['operation'] != operation or pending['intent']['unit'] != unit
            or identity['unit'] != unit or p['InvocationID'] != identity['invocation_id']
            or p['ExecMainPID'] != str(identity['main_pid']) or p['SubState'] != 'failed'):
        raise ValueError('original worker differs')
    stem = REMOTE+'/transfer-'+operation+'.jsonl'
    preflight, journal = fetch(stem+'.preflight.json',True), fetch(stem,True)
    events = [json.loads(line) for line in journal.splitlines()]
    if [e['phase'] for e in events] == ['intent','outcome-uncertain'] and json.loads(preflight)['expected'] == expected:
        raise IncompleteGwsStage('GWS stage was interrupted')
    if [e['phase'] for e in events] != ['intent','stage-complete'] or json.loads(preflight)['expected'] != expected:
        raise ValueError('original worker did not finish staging')
    resume_name='aurora-direct-'+resume
    resume_unit=resume_name+'.service'
    if state(resume_unit)['LoadState'] == 'not-found':
        check_capacity(expected['size'])
    request = json.loads(fetch(prep['out']+'/request.json'))
    request['operation_id'] = resume
    bundle = REMOTE+'/exact-stage-resume-'+stamp.lower()
    continuity, observation_raw = recovery_inputs(bundle, expected)
    common = {
        'gws-only-103414z-0':'gws-only-'+stamp.lower()+'-0',
        'gws-resume-103414z-0':'gws-resume-'+stamp.lower()+'-0',
        '8b9b1d08a732f14095589b49c8528c1af15e9bfa24293eaeb34ae7f611165092':sha(preflight),
        'caee1fc22e844d0a54c0e2dd9cf752b79376b335aff6db6f414cb09c71d71e1f':sha(journal),
    }
    replacements = {
        **common,'7368ca8b53254d2f8effa6ad123ee6df':identity['invocation_id'],
        '897345':str(identity['main_pid']),
    }
    if release['id'] in {'gzip-v4-vm01','gzip-v5-vm01-lanes'}:
        replacements['VM01_LAUNCHER_SHA256_PLACEHOLDER'] = release['pins'][release['launcher']]
    verify_resume_template(release)
    launcher = pinned_template(release['resume_template'],replacements).encode()
    files = {'launcher.py':launcher,'request.json':json.dumps(request,sort_keys=True).encode(),
             'continuity.json':continuity,'observation.json':observation_raw}
    args = ['/usr/bin/python3',bundle+'/launcher.py']
    for name in ('request','continuity','observation'):
        args += ['--'+name,bundle+'/'+name+'.json','--'+name+'-sha256',sha(files[name+'.json'])]
    for option,name in (('resume','resume_verified_gws_stage.py'),('worker','verify_failed_stage_worker.py')):
        pin=sha((HERE/name).read_bytes())
        actual=run_remote(['sudo','-n','sha256sum',REMOTE+'/'+name]).stdout.decode().split()[0]
        if actual != pin:
            raise ValueError('installed recovery dependency differs')
        args += ['--'+option+'-module-sha256',pin]
    files['launcher-args.json']=json.dumps(args).encode()
    files['SHA256SUMS']=''.join(f'{sha(raw)}  {name}\n' for name,raw in files.items()).encode()
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as archive:
        for name,raw in files.items():
            entry=tarfile.TarInfo(name);entry.mode=0o600;entry.size=len(raw)
            archive.addfile(entry,io.BytesIO(raw))
    # Install the complete bundle before either systemd unit can be launched.
    run_remote(['sudo','-n','sh','-ec', '''
umask 077
if [ ! -e "$1" ]; then
    staging=$(mktemp -d "$1.tmp.XXXXXX")
    trap 'rm -rf -- "$staging"' EXIT
    tar --no-same-owner -xf - -C "$staging"
    (cd "$staging"; sha256sum --quiet -c SHA256SUMS)
    sync -f "$staging"
    mv -T --no-clobber "$staging" "$1"
    sync -f "$(dirname "$1")"
fi
cd "$1"
sha256sum --quiet -c SHA256SUMS
''','sh',bundle],data=stream.getvalue())
    if fetch(bundle+'/launcher.py',True) != launcher or fetch(bundle+'/request.json',True) != files['request.json']:
        raise ValueError('installed recovery bundle differs from original worker')
    saved_args = json.loads(fetch(bundle+'/launcher-args.json',True))
    for name in ('continuity', 'observation'):
        args[args.index('--'+name+'-sha256')+1] = sha(fetch(bundle+'/'+name+'.json',True))
    if saved_args != args:
        raise ValueError('installed recovery arguments differ')
    admission='aurora-direct-stage-resume-admission-'+stamp.lower()
    def launch(name, arguments):
        run_remote(['sudo','-n','systemd-run','--unit='+name,'--property=Type=exec',
                    '--property=RemainAfterExit=yes','--property=RuntimeMaxSec=90min','--nice=15',*arguments])
    if recorded_worker(admission, args) is None:
        launch(admission,args)
    admitted=observe_worker(admission,args,1200)
    lines=run_remote(['sudo','-n','journalctl','-u',admission+'.service','_SYSTEMD_INVOCATION_ID='+admitted['InvocationID'],
                      '-o','cat','--no-pager']).stdout.decode().splitlines()
    proof = {'mode':'read-only','stage_verified':True,'gws_verified':False,'object_verified':False,
             'retention_authorized':False,'eviction_authorized':False}
    if sum(line.startswith('{') and json.loads(line)==proof for line in lines)!=1:
        raise ValueError('independent staged admission evidence missing')
    resume_args=[*args,'--apply']
    launch_intent = prep['out']+'/recovery-launch-intent.json'
    if recorded_worker(resume_name, resume_args) is None:
        exists = run_remote(['python3', '-c', 'import os,sys;print(int(os.path.lexists(sys.argv[1])))',
                             launch_intent]).stdout.strip()
        if exists != b'0':
            raise ValueError('reserved recovery worker disappeared')
        check_capacity(expected['size'])
        save_recovery_record(launch_intent, {'operation_id':pending['id'], 'unit':resume_unit,
                                             'launcher_sha256':sha(launcher), 'argv':resume_args})
        launch(resume_name,resume_args)
    p=recorded_worker(resume_name,resume_args)
    if p is None:
        raise ValueError('launched recovery worker cannot be identified')
    resume_identity={'unit':resume_unit,'invocation_id':p['InvocationID'],'main_pid':int(p['ExecMainPID'])}
    save_recovery_record(prep['out']+'/recovered-service-identity.json', resume_identity)
    print('Recovery worker',json.dumps(resume_identity),flush=True)
    done=observe_worker(resume_name,resume_args,5400)
    if done['InvocationID']!=p['InvocationID'] or done['ExecMainPID']!=p['ExecMainPID']:
        raise ValueError('recovery worker invocation differs')
    capture=pinned_template('capture_recovered_gws_103414.py',{
        **common,'20261107T103414Z':stamp,'/resume_stage_launcher_103414.py':bundle.removeprefix(REMOTE)+'/launcher.py',
        'from capture_gws_only import BASE, REMOTE, save':
            'from capture_gws_only_refresh import BASE, REMOTE, save',
        'out.mkdir(mode=0o700)':'out.mkdir(mode=0o700, exist_ok=True)',
    })
    capture += '''
from contextlib import nullcontext
from per_file_queue import writer_lock
from gws_pending_ledger import register_captured
owned = sys.argv[3] == 'coordinator-owned'
with (nullcontext() if owned else writer_lock(BASE/'per-file-coordinator.lock')), (nullcontext() if owned else writer_lock(BASE/'bounded-backlog-coordinator.lock')):
    identity = json.loads(sys.argv[1])
    proof = capture(identity,sys.argv[2])
    record = register_captured(PREPARATION,BASE/'gws-only-pending.sqlite')
    print(json.dumps({'expected':proof['expected'],'registered':record},sort_keys=True))
'''
    result=run_remote(['env','RADAR_COORDINATOR_ROOT='+ROOT,'RADAR_COORDINATOR_LOCAL=1',
                       'PYTHONPATH='+str(HERE),'PYTHONDONTWRITEBYTECODE=1',
                       'python3','-c','import sys\n'+capture,json.dumps(resume_identity),sha(launcher),
                       'coordinator-owned' if coordinator_owned else 'standalone'],timeout=300)
    print(result.stdout.decode(),flush=True)


if __name__ == '__main__':
    run()
