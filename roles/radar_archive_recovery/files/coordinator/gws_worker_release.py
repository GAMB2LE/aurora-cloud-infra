"""Allowlisted GWS worker releases, bound to each durable preparation.

Release selection affects new preparations only. Existing attempts always use
their recorded launcher, so a later default can never move recovery across hosts.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex

ROOT = '/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027'
JOURNALS = ROOT+'/direct-staged-v1'
OLD_LONG = ROOT+'/direct-staged-gzip-v3'
VM01_ROOT = ROOT+'/direct-staged-gzip-v4-vm01'
VM01_MANIFEST_SHA256 = '5b127ec27d176cf03ce7157e90bc323a31a27d8e5688d8aa04e4662bd81ae07f'
VM01_MANIFEST = Path(__file__).with_name('gws_worker_release_v4.json')
LANE_ROOT = ROOT+'/direct-staged-gzip-v5-vm01-lanes'
LANE_MANIFEST = Path(__file__).with_name('gws_worker_release_v5.json')
LANE_MANIFEST_SHA256 = '0fe69b7c5e9002461a0c99ff4b1eb0b562c2d5f756f9d6a9f2263b09c867f4fa'
COMMON = {
    'receiver_sha256':'521eeb16d10d59926435935092c9eb4b1523fca0b5be28f1fbd16f1887dd6e37',
    'continuity_verifier':{'path':JOURNALS+'/verify_released_radar_continuity.py',
        'sha256':'a956e6c556dbf9a57c85983d1878c92d4bbd8cd3b7508957245f18f2caea44d7'},
    'continuity_acceptance':{'path':JOURNALS+'/released-radar-acceptance.json',
        'sha256':'f0edc78e2d99775dff9eff914f8eadd053e98b57f4e8346b8add8953d651f238'},
}
STANDARD = {**COMMON, 'id':'refresh-v1-vm03', 'bundle_root':JOURNALS,
    'launcher':JOURNALS+'/direct_staged_launcher_refresh.py',
    'pins':{JOURNALS+'/direct_staged_launcher_refresh.py':
        'f8ee115bf52e3c9df107dcab09dde2bf9d9fe6ca4ad74817873905ee1c3d2142'},
    'endpoint':'rrniii@xfer-vm-03.jasmin.ac.uk','hostname':'xfer-vm-03.jasmin.ac.uk',
    'resume_template':'resume_stage_launcher_103414.py','fence_launcher':None}
LONG = {**COMMON, 'id':'gzip-v3-vm03', 'bundle_root':OLD_LONG,
    'launcher':OLD_LONG+'/direct_staged_launcher_gzip.py',
    'endpoint':'rrniii@xfer-vm-03.jasmin.ac.uk','hostname':'xfer-vm-03.jasmin.ac.uk',
    'resume_template':'resume_stage_launcher_103414.py','fence_launcher':None,
    'pins':{OLD_LONG+'/'+name:pin for name,pin in {
        'direct_staged_launcher_gzip.py':'a00398bfd5d3a1b8b47e8b8157cbc9b444241da26fa3bc80c9bd722491f224fa',
        'direct_stage_pipeline.py':'45e81ded6b9296f20fc20724991d6156aa03dccd6115d314558e93bb074abffb',
        'direct_staged_recovery_refresh.py':'3e7804141a668e333569b29680e282f457da5028affad4d2a601af9866416c4b',
        'gzip_gws_cloud_adapter.py':'4ef9046b32144a1d6eb4779b8229be196b4002413cdff6c2566ac6f915985b55',
        'gws_gzip_stage.py':'c7f4b8f2ba27aa99a1ab47a5fadcf5d8e7e6c6b1454fe2ce5fce3204dac083da',
        'compressed_source_stream.py':'4608017870d92e1095b5b13f49d8a746317f3f323dc110c257aedd5b953d0a55',
    }.items()}}


def releases():
    result = {item['id']:copy.deepcopy(item) for item in (STANDARD,LONG)}
    if VM01_MANIFEST_SHA256 != 'UNBUILT':
        raw = VM01_MANIFEST.read_bytes()
        if hashlib.sha256(raw).hexdigest() != VM01_MANIFEST_SHA256:
            raise ValueError('GWS release manifest pin differs')
        new = json.loads(raw)
        if (new.get('id') != 'gzip-v4-vm01' or new.get('bundle_root') != VM01_ROOT
                or new.get('endpoint') != 'rrniii@xfer-vm-01.jasmin.ac.uk'
                or new.get('hostname') != 'xfer-vm-01.jasmin.ac.uk'
                or new.get('launcher') != VM01_ROOT+'/direct_staged_launcher_gzip.py'
                or any(new.get(key) != value for key,value in COMMON.items())
                or new.get('resume_template') != 'resume_stage_launcher_vm01.py'
                or not re.fullmatch('[0-9a-f]{64}',new.get('resume_template_sha256',''))
                or new.get('fence_launcher') != VM01_ROOT+'/gws_fence_recovery_launcher_vm01.py'):
            raise ValueError('invalid vm01 GWS release manifest')
        if (new['launcher'] not in new.get('pins', {}) or new['fence_launcher'] not in new.get('pins', {}) or not new['pins']
                or any(not path.startswith(VM01_ROOT+'/') or not re.fullmatch('[0-9a-f]{64}',pin)
                       for path,pin in new['pins'].items())):
            raise ValueError('invalid vm01 component pins')
        result[new['id']] = new
    raw = LANE_MANIFEST.read_bytes()
    if hashlib.sha256(raw).hexdigest() != LANE_MANIFEST_SHA256:
        raise ValueError('lane GWS release manifest pin differs')
    lane = json.loads(raw)
    if (lane.get('id') != 'gzip-v5-vm01-lanes' or lane.get('bundle_root') != LANE_ROOT
            or lane.get('endpoint') != 'rrniii@xfer-vm-01.jasmin.ac.uk'
            or lane.get('hostname') != 'xfer-vm-01.jasmin.ac.uk'
            or lane.get('launcher') != LANE_ROOT+'/direct_staged_launcher_gzip.py'
            or lane.get('fence_launcher') != LANE_ROOT+'/gws_fence_recovery_launcher_vm01.py'
            or lane.get('resume_template') != 'resume_stage_launcher_vm01_lanes.py'
            or not re.fullmatch('[0-9a-f]{64}', lane.get('resume_template_sha256', ''))
            or any(lane.get(key) != value for key,value in COMMON.items())
            or lane['launcher'] not in lane.get('pins', {}) or lane['fence_launcher'] not in lane['pins']
            or any(not path.startswith(LANE_ROOT+'/') or not re.fullmatch('[0-9a-f]{64}', pin)
                   for path,pin in lane['pins'].items())):
        raise ValueError('invalid lane GWS release manifest')
    result[lane['id']] = lane
    return result


def select_release(long_worker):
    chosen = os.environ.get('RADAR_GWS_WORKER_RELEASE')
    if chosen is None:
        chosen = LONG['id'] if long_worker else STANDARD['id']
    if chosen not in releases() or (chosen != STANDARD['id'] and not long_worker):
        raise ValueError('unavailable GWS worker release')
    return releases()[chosen]


def record_release(preparation, release):
    if releases().get(release.get('id')) != release:
        raise ValueError('unrecognized GWS release record')
    path = Path(preparation)/'worker-release.json'
    raw = (json.dumps(release,sort_keys=True)+'\n').encode()
    fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
    fd = os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def resolve_preparation(preparation, intent_preparation=None, authenticate=None):
    preparation = Path(preparation)
    argv = json.loads((preparation/'launcher-args.json').read_bytes())
    allowed = releases()
    matches = [release for release in allowed.values() if len(argv)>=2 and argv[1]==release['launcher']]
    if len(matches)!=1:
        raise ValueError('unrecognized GWS launcher')
    release = matches[0]
    if len(argv)!=14 or argv[0]!='/usr/bin/python3':
        raise ValueError('invalid GWS launcher arguments')
    for offset,name in ((2,'request'),(6,'continuity'),(10,'observation')):
        if (argv[offset]!='--'+name or argv[offset+2]!='--'+name+'-sha256'
                or not isinstance(argv[offset+1],str)
                or not re.fullmatch(re.escape(JOURNALS)+r'/input-20\d{6}t\d{6}z/'+name+r'\.json',argv[offset+1])
                or not isinstance(argv[offset+3],str) or not re.fullmatch('[0-9a-f]{64}',argv[offset+3])):
            raise ValueError('invalid GWS launcher arguments')
    parents={str(Path(argv[index]).parent) for index in (3,7,11)}
    if len(parents)!=1:
        raise ValueError('mixed GWS input bundles')
    record = preparation/'worker-release.json'
    if os.path.lexists(record):
        if record.is_symlink() or json.loads(record.read_bytes())!=release:
            raise ValueError('recorded GWS release differs')
    elif release['id'] in {'gzip-v4-vm01','gzip-v5-vm01-lanes'}:
        raise ValueError('vm01 GWS release record missing')
    if intent_preparation is not None:
        recorded = intent_preparation.get('worker_release')
        if recorded != release['id'] and (recorded is not None or release['id'] in {'gzip-v4-vm01','gzip-v5-vm01-lanes'}):
            raise ValueError('queue GWS release differs')
    identity = json.loads((preparation/'service-identity.json').read_bytes()) if authenticate else None
    if authenticate:
        authenticate(identity['unit'],argv)
    return {**release,'launcher_args':argv}


def authenticate_worker(unit, argv, run_remote):
    if not re.fullmatch(r'aurora-direct-20261027-gws-only-(?:20\d{6}t)?\d{6}z-0\.service',unit):
        raise ValueError('invalid GWS worker unit')
    command=run_remote(['systemctl','show',unit,'-p','ExecStart','--value']).stdout.decode()
    # Include the systemd field delimiter so a path prefix or extra flags cannot
    # masquerade as the authenticated command.
    if 'argv[]='+shlex.join([*argv,'--apply'])+' ;' not in command:
        raise ValueError('GWS worker command differs from recorded release')


def verify_release_pins(release, run_remote):
    for path,pin in release['pins'].items():
        result=run_remote(['sudo','-n','sha256sum',path],timeout=60).stdout.decode().split()
        if len(result)!=2 or result[0]!=pin or result[1]!=path:
            raise ValueError('GWS release component differs: '+path)
