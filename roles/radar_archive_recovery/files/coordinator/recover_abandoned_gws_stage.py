"""Preserve an unpublished partial and retire the exact failed queue operation."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time

from bounded_backlog_runner import state
from capture_gws_only_refresh import save
from per_file_queue import AUTHORITY, valid_expected
from prepare_direct_candidate import BASE, run_remote
from reconcile_gws_first_pair import check_retention_and_fence
from gws_worker_release import resolve_preparation, authenticate_worker, verify_release_pins

JOURNAL_ROOT = '/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027/direct-staged-v1'
LOCK_ROOT = Path('/data/aurora/internal/archive_dispatch/gws-transfer')
DELIVERY_HELPER = Path('/usr/local/lib/aurora-gws-transfer')
DELIVERY_SHA256 = 'db8d5b671e91032ffec0afd9d62081969fcb6ce7d7fecee0d92fd630d2bfc987'
RECEIVER_SHA256 = '521eeb16d10d59926435935092c9eb4b1523fca0b5be28f1fbd16f1887dd6e37'

REMOTE = r'''
import json, os, pathlib, stat, sys, time
relative, nonce, size = sys.argv[1:]
size = int(size)
root = pathlib.Path('/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/rpgfmcw94')
target = root/relative
name = '.' + target.name + '.' + nonce + '.partial'
preserved = '.interrupted-' + target.name + '.' + nonce
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
fd = os.open('/', flags)
try:
    for part in target.parent.parts[1:]:
        child = os.open(part, flags, dir_fd=fd)
        os.close(fd)
        fd = child

    def lookup(entry):
        try:
            return os.stat(entry, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def signature(entry):
        return [entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns]

    def check_receiver():
        for proc in pathlib.Path('/proc').iterdir():
            if not proc.name.isdigit() or int(proc.name) == os.getpid():
                continue
            try:
                if proc.stat().st_uid == os.getuid() and nonce.encode() in (proc/'cmdline').read_bytes().split(b'\0'):
                    raise ValueError('matching receiver is still live')
            except (FileNotFoundError, ProcessLookupError):
                continue

    def observed():
        return [None if entry is None else (signature(entry), entry.st_ctime_ns, entry.st_nlink)
                for entry in (lookup(name), lookup(preserved))]

    check_receiver()
    before = observed()
    time.sleep(2)
    check_receiver()
    if observed() != before:
        raise ValueError('partial is still changing')
    if lookup(target.name) is not None:
        raise ValueError('canonical GWS path exists')
    original, saved = lookup(name), lookup(preserved)
    others = [n for n in os.listdir(fd) if n.startswith('.' + target.name + '.')
              and n.endswith('.partial') and n != name]
    if others:
        raise ValueError('another partial requires reconciliation')
    if original is None and saved is None:
        print(json.dumps({'partial_identity':None,'preserved_name':None,'canonical_present':False}))
        sys.exit(0)
    expected = signature(original if original is not None else saved)
    for entry in (original, saved):
        if entry is not None and (not stat.S_ISREG(entry.st_mode) or signature(entry) != expected
                                  or entry.st_uid != os.getuid() or not 0 <= entry.st_size <= size):
            raise ValueError('partial identity changed')
    if original is not None and saved is None:
        if original.st_nlink != 1:
            raise ValueError('partial unexpectedly linked')
        os.link(name, preserved, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
        saved = lookup(preserved)
    if saved is None or signature(saved) != expected:
        raise ValueError('preserved partial missing')
    if original is not None:
        current = lookup(name)
        if signature(current) != expected or current.st_nlink != 2:
            raise ValueError('partial changed before quarantine')
        os.unlink(name, dir_fd=fd)
    os.fsync(fd)
    saved = lookup(preserved)
    if saved.st_nlink != 1 or lookup(target.name) is not None:
        raise ValueError('quarantine result differs')
    print(json.dumps({'partial_identity':signature(saved),'preserved_name':preserved,'canonical_present':False}))
finally:
    os.close(fd)
'''


def recover(queue, op):
    helper = DELIVERY_HELPER.read_bytes()
    if hashlib.sha256(helper).hexdigest() != DELIVERY_SHA256:
        raise ValueError('installed delivery helper differs')
    namespace = {'__name__': 'installed_fenced_delivery', '__file__': str(DELIVERY_HELPER)}
    exec(compile(helper, str(DELIVERY_HELPER), 'exec'), namespace)
    delivery_lock = namespace['delivery_lock']
    if queue.outstanding() != op or op['target'] != 'gws-verified':
        raise ValueError('different queue operation')
    expected = op['expected']
    if not valid_expected(expected):
        raise ValueError('invalid exact GWS source')
    path = Path(op['intent']['preparation']['out'])
    match = re.fullmatch(r'direct-gws-only-preparation-(20\d{6}T\d{6}Z)', path.name)
    if path.parent != BASE or not match:
        raise ValueError('unexpected GWS preparation path')
    operation = '20261027-gws-only-'+match[1].lower()+'-0'
    if op['intent']['unit'] != 'aurora-direct-'+operation+'.service':
        raise ValueError('unexpected GWS worker name')
    quarantine = path.with_name('interrupted-'+path.name)
    prep = path if path.is_dir() else quarantine
    if not prep.is_dir() or prep.is_symlink():
        raise ValueError('invalid GWS recovery preparation')
    identity = json.loads((prep/'service-identity.json').read_bytes())
    # The preparation and queue intent must select the same reviewed release.
    # No worker from a different release may be adopted, even if its unit name
    # and invocation match.
    release = resolve_preparation(prep, intent_preparation=op['intent']['preparation'])
    if release['endpoint'] not in ('rrniii@xfer-vm-03.jasmin.ac.uk', 'rrniii@xfer-vm-01.jasmin.ac.uk'):
        raise ValueError('unreviewed recovery endpoint')
    stem = JOURNAL_ROOT+'/transfer-'+operation+'.jsonl'
    unit = op['intent']['unit']
    worker = state(unit)
    if (identity['unit'] != unit or worker['SubState'] != 'failed' or worker['Result'] != 'exit-code'
            or worker['ExecMainPID'] != str(identity['main_pid'])
            or worker['InvocationID'] != identity['invocation_id']
            or run_remote(['systemctl','show',unit,'-p','ControlGroup','--value']).stdout.strip()):
        raise ValueError('original worker identity differs')
    authenticate_worker(unit, release['launcher_args'], run_remote)
    verify_release_pins(release, run_remote)
    for suffix in ('.commit.jsonl', '.readback.json', '.fence-reconciliation.json'):
        run_remote(['sudo','-n','test','!','-e',stem+suffix])
    pre = run_remote(['sudo','-n','cat',stem+'.preflight.json']).stdout
    raw = run_remote(['sudo','-n','cat',stem]).stdout
    events = [json.loads(line) for line in raw.splitlines()]
    manifest = json.loads(pre)
    nonce = manifest['operation']['nonce']
    if (manifest['expected'] != expected or not re.fullmatch('[0-9a-f]{32}', nonce)
            or manifest['operation'].get('destination') != 'gws-canonical'
            or manifest['operation'].get('receiver_script_sha256') != release['receiver_sha256']
            or any(manifest.get(k) is not v for k,v in AUTHORITY.items())
            or [row['phase'] for row in events] != ['intent','outcome-uncertain']
            or events[0]['operation'] != manifest['operation']
            or any(row.get(k) != v for row in events for k,v in expected.items())
            or any(row.get(k) is not v for row in events for k,v in AUTHORITY.items())):
        raise ValueError('interrupted journal differs')
    with delivery_lock(LOCK_ROOT, 'fast', wait_seconds=10):
        check_retention_and_fence()
        cmd = ['sudo','-n','-u','aurora','--','ssh','-T','-o','BatchMode=yes',
               '-o','StrictHostKeyChecking=yes','-o','IdentitiesOnly=yes',
               '-i','/home/aurora/.ssh/id_rsa_jasmin_20200514',release['endpoint'],
               shlex.join(['python3','-c',REMOTE,expected['relative_path'],nonce,str(expected['size'])])]
        proof = json.loads(run_remote(cmd,timeout=30).stdout)
        receipt = {'schema':'radar-interrupted-gws-attempt-v1','operation_id':op['id'],
                   'expected':expected,'worker':worker,'partial':proof,
                   'preflight_sha256':hashlib.sha256(pre).hexdigest(),
                   'journal_sha256':hashlib.sha256(raw).hexdigest(),**AUTHORITY}
        save(prep/'interrupted-transfer.json', (json.dumps(receipt,sort_keys=True)+'\n').encode())
        if prep == path:
            if os.path.lexists(quarantine):
                raise ValueError('preparation quarantine exists')
            prep.rename(quarantine)
            fd = os.open(BASE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        with queue.db:
            result = queue.db.execute('UPDATE operations SET finished=?,receipt=? WHERE id=? AND finished IS NULL',
                                      (time.time(),json.dumps(receipt,sort_keys=True),op['id']))
            if result.rowcount != 1:
                raise ValueError('queue operation changed')
        print(json.dumps(receipt,sort_keys=True))
