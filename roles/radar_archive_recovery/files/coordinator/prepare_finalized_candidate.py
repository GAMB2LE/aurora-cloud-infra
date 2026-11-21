"""Prepare a finalized-stage GWS admission without launching a transfer."""
import datetime
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import tarfile

from finalized_expected import valid_expected
from finalized_gws_adapter import BOOTSTRAP, PINS
from prepare_gws_only_candidate_refresh import BASE, ROOT, INFRA, REMOTE, run_remote, verify_remote_hashes
from gws_only_runner import exclusive_json
from bounded_backlog_runner import admission

LAUNCHER = '/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/direct-finalized-gws-v4/finalized_gws_launcher.py'
LAUNCHER_SHA = '436015e61f11eb4c007005a3ebc904fabdb7c7cf9a0531e6ae021c23b0ae18ee'
FIELD_HASH = r'''import hashlib,json,os,pathlib,stat,sys
p=pathlib.Path(sys.argv[1])
if not p.is_absolute() or '..' in p.parts or any(a.is_symlink() for a in (p,*p.parents)): raise ValueError('unsafe source path')
def sig(s): return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_uid,s.st_gid,s.st_mode,s.st_nlink,s.st_blocks]
before=p.lstat()
if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: raise ValueError('source is not an ordinary file')
h=hashlib.sha256()
with os.fdopen(os.open(p,os.O_RDONLY|os.O_NOFOLLOW),'rb') as f:
 if sig(os.fstat(f.fileno()))!=sig(before): raise ValueError('source replaced')
 while chunk:=f.read(8*1024**2): h.update(chunk)
 if sig(os.fstat(f.fileno()))!=sig(before): raise ValueError('source changed during read')
if sig(p.lstat())!=sig(before): raise ValueError('source changed after read')
print(json.dumps({'identity':sig(before),'sha256':h.hexdigest()}))
'''


def build_request(template, handoff, source, operation):
    expected = {'relative_path': handoff.get('source'), 'size': handoff.get('size'),
                'mtime_ns': handoff.get('source_mtime_ns'), 'sha256': handoff.get('sha256')}
    identity = source.get('identity') if isinstance(source, dict) else None
    if (not valid_expected(expected) or not isinstance(identity, list) or len(identity) != 10
            or any(type(value) is not int or value < 0 for value in identity)
            or identity[2:4] != [expected['size'], expected['mtime_ns']]
            or source.get('sha256') != expected['sha256']
            or any(not isinstance(handoff.get(k), str) or not re.fullmatch('[0-9a-f]{32}', handoff[k])
                   for k in ('stage_operation', 'post_stage_operation'))
            or not re.fullmatch(r'20261027-gws-only-20\d{6}t\d{6}z-0', operation)):
        raise ValueError('source version or handoff differs')
    request = dict(template)
    request.update(expected_source=source, relative_path=expected['relative_path'], operation_id=operation,
                   finalized_stage={'operation': handoff['stage_operation'],
                                    'post_stage_operation': handoff['post_stage_operation'], 'expected': expected})
    request['archive_dispatch_sha256'] = '750bcaad0881f057e6f8f2cd1f9c6ef298182c8aa5abc69d45e0c31947ba8043'
    return request


def field(command):
    ssh = ['sudo', '-n', '-u', 'aurora', 'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=12',
           '-o', 'IdentitiesOnly=yes', '-o', 'IdentityFile=none', '-o', 'PubkeyAuthentication=no',
           '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=/home/aurora/.ssh/known_hosts',
           'aurora@100.124.55.22', shlex.join(command)]
    return json.loads(run_remote(ssh, timeout=1800).stdout)


def prepare(stage_operation, after):
    if any(not re.fullmatch('[0-9a-f]{32}', x) for x in (stage_operation, after)):
        raise ValueError('invalid handoff identity')
    result = run_remote(['sudo', '-n', 'sha256sum', LAUNCHER], timeout=60).stdout.decode().split()
    if result != [LAUNCHER_SHA, LAUNCHER]: raise ValueError('finalized launcher pin differs')
    inspect = ['python3', '-c', BOOTSTRAP, json.dumps(PINS, sort_keys=True), 'inspect', stage_operation, after]
    handoff = field(inspect)
    name = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    operation = '20261027-gws-only-' + name.lower() + '-0'
    out = BASE / ('direct-finalized-preparation-' + name)
    out.mkdir(mode=0o700)
    source = field(['python3', '-c', FIELD_HASH, '/home/aurora/data/rpgfmcw94/' + handoff['source']])
    template = json.loads((BASE / 'direct-object-gzip-preparation-20261106T2249Z/request.json').read_bytes())
    request = build_request(template, handoff, source, operation)
    accept = (ROOT / 'output/aurora-archive-repair-20261026/release-20261027T005416Z/storage-review-release-completed-accepted.json').read_bytes()
    code = (INFRA / 'scripts/verify_released_radar_continuity.py').read_text()
    raw = run_remote(['sudo', '-n', 'python3', '-c', code, '--acceptance-sha256',
                      hashlib.sha256(accept).hexdigest()], data=accept).stdout
    continuity = json.loads(raw)
    if continuity.get('accepted') is not True or continuity.get('files') != 640:
        raise ValueError('historical continuity refused')
    if field(inspect) != handoff:
        raise ValueError('authenticated handoff changed during preparation')
    exclusive_json(out / 'request.json', request)
    exclusive_json(out / 'continuity.json', continuity)
    names = ('request.json', 'continuity.json')
    hashes = {n: hashlib.sha256((out / n).read_bytes()).hexdigest() for n in names}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for name_ in names: tar.add(out / name_, arcname=name_)
    directory = REMOTE + '/input-' + name.lower()
    command = ('umask 077; mkdir ' + shlex.quote(directory) + ' && tar -xf - -C ' + shlex.quote(directory)
               + ' && chown root:root ' + shlex.quote(directory) + '/*.json && chmod 600 '
               + shlex.quote(directory) + '/*.json && sha256sum ' + shlex.quote(directory) + '/*.json')
    remote = run_remote(['sudo', '-n', 'sh', '-c', command], data=archive.getvalue())
    verify_remote_hashes(remote.stdout, directory, hashes)
    args = ['/usr/bin/python3', LAUNCHER, '--request', directory + '/request.json', '--request-sha256', hashes['request.json'],
            '--continuity', directory + '/continuity.json', '--continuity-sha256', hashes['continuity.json']]
    exclusive_json(out / 'launcher-args.json', args)
    unit = 'aurora-finalized-admission-' + name.lower() + '.service'
    run_remote(['sudo', '-n', 'systemd-run', '--unit=' + unit.removesuffix('.service'), '--property=Type=exec',
                '--property=RemainAfterExit=yes', '--property=RuntimeMaxSec=20min', '--nice=15', *args])
    proof = admission(unit, request)
    exclusive_json(out / 'admission-evidence.json', {'unit': unit, **proof,
                   'retention_authorized': False, 'eviction_authorized': False})
    return {'out': str(out), 'operation': operation, 'expected': request['finalized_stage']['expected'],
            'admission_unit': unit,
            'transfer_started': False, 'retention_authorized': False, 'eviction_authorized': False}
