#!/usr/bin/env python3
"""Rehearse registered radar replacements in a private mount namespace only."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

BASE = Path('/var/lib/aurora-retention-return-20261121')
LAYOUT = Path('/var/lib/aurora-radar-coordinator-v1/overnight-20261114/relocated-may-d13-cutover-candidate/verify_radar_mount_layout.py')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, timeout=600, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--day', action='append', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or os.readlink('/proc/self/ns/mnt') == os.readlink('/proc/1/ns/mnt'):
        raise PermissionError('run as root under unshare --mount --propagation private')
    spec = importlib.util.spec_from_file_location('retention', '/usr/local/lib/aurora-radar-retention-layout.py')
    helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
    if set(args.day) - helper.DAYS or len(set(args.day)) != len(args.day):
        raise ValueError('unreviewed or duplicate rehearsal day')
    release = json.loads((args.release / 'release.json').read_text())
    for row in release['changes']:
        source = args.release / row['path'].lstrip('/')
        if hashlib.sha256(source.read_bytes()).hexdigest() != row['after_sha256']:
            raise ValueError('staged release component differs')
    with tempfile.TemporaryDirectory(prefix='layout-rehearsal-', dir=BASE, ignore_cleanup_errors=True) as temporary:
        scratch = Path(temporary)
        etc = scratch / 'etc-aurora'; etc.mkdir()
        original_controls = {}
        for name in ('radar-relocation-active.json', 'radar-relocation-plan.json',
                     'ass-retention.hold', 'radar-may-d13-compressed.json'):
            original_controls[name] = os.open(Path('/etc/aurora') / name, os.O_RDONLY | os.O_NOFOLLOW)
            (etc / name).touch()
        routes = {}
        for day in args.day:
            folder = helper.IMAGES / day.replace('/', '-')
            receipt = (folder / 'verified.json').read_bytes()
            proof = json.loads(receipt)
            routes[day] = {'image_sha256': proof['image_sha256'],
                'image_identity': helper.signature((folder / 'archive.btrfs').stat()),
                'receipt_sha256': hashlib.sha256(receipt).hexdigest(),
                'relocation_manifest_sha256': proof['relocation_proof_sha256']}
        (etc / 'radar-retention-compressed.json').write_text(json.dumps({'schema_version': 1, 'routes': routes}))
        run('mount', '--bind', str(etc), '/etc/aurora')
        # Preserve the real control inode identities pinned by historical mounted
        # verification. Copying ACTIVE would correctly invalidate that proof.
        for name, fd in original_controls.items():
            # Let the kernel follow the open file description. libmount's usual
            # realpath canonicalization would instead select the shadowed pathname.
            run('mount', '--no-canonicalize', '--bind', '/proc/' + str(os.getpid()) + '/fd/' + str(fd), '/etc/aurora/' + name)
            run('mount', '-o', 'remount,bind,ro', '/etc/aurora/' + name)
            expected, visible = os.fstat(fd), os.stat('/etc/aurora/' + name)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if identity(expected) != identity(visible):
                raise ValueError('private namespace changed a pinned control identity')
        # Bind staged code only in this namespace. Existing production processes
        # keep their current files, paths and mounts throughout the rehearsal.
        for row in release['changes']:
            if row['path'] == '/usr/local/lib/aurora-radar-retention-layout.py':
                continue
            run('mount', '--bind', str(args.release / row['path'].lstrip('/')), row['path'])
        checked = {}
        for day in args.day:
            target = helper.RAW / day
            run('umount', str(target))
            image = helper.IMAGES / day.replace('/', '-') / 'archive.btrfs'
            run('mount', '-t', 'btrfs', '-o', 'loop,ro,nologreplay,nodev,nosuid,noexec', str(image), str(target))
            checked[day] = helper.guard(day, routes[day])
            proof = json.loads((helper.PROOFS / (day.replace('/', '-') + '.json')).read_text())
            samples = {}
            for suffix in ('.LV0', '.LV1', '.LV0.NC', '.LV1.NC'):
                name = min((n for n in proof['files'] if n.endswith(suffix)), key=lambda n: proof['files'][n]['size'])
                expected = proof['files'][name]['sha256']
                actual = subprocess.check_output(['sha256sum', '--', str(target / name)], text=True, timeout=300).split()[0]
                if actual != expected:
                    raise ValueError('canonical-path restored sample differs')
                samples[name] = actual
            checked[day]['canonical_path_sha256_samples'] = samples
        result = json.loads(subprocess.check_output(['/usr/bin/python3', str(LAYOUT)], text=True, timeout=180))
        if result['legacy_bind_days'] + len(result.get('retention_compressed_routes', {})) != 6:
            raise ValueError('historical cohort coverage changed')
        acceptance = Path('/var/lib/aurora-radar-coordinator-v1/output/aurora-archive-repair-20261026/release-20261027T005416Z/storage-review-release-completed-accepted.json').read_bytes()
        if hashlib.sha256(acceptance).hexdigest() != 'f0edc78e2d99775dff9eff914f8eadd053e98b57f4e8346b8add8953d651f238':
            raise ValueError('historical release acceptance changed')
        try:
            continuity_raw = run('/usr/bin/python3', str(LAYOUT.with_name('verify_released_radar_continuity.py')),
                '--acceptance-sha256', hashlib.sha256(acceptance).hexdigest(),
                input=acceptance, capture_output=True).stdout
        except subprocess.CalledProcessError as error:
            raise ValueError('historical continuity probe failed: ' + error.stderr.decode()[:2000]) from error
        continuity = json.loads(continuity_raw)
        if (continuity.get('accepted') is not True or continuity.get('files') != 640
                or set(continuity.get('retention_compressed_days', [])) != set(args.day)
                or continuity.get('release_authorized') is not False):
            raise ValueError('whole-cohort historical continuity failed')
        record = {'schema': 'private-retention-layout-rehearsal-v1',
                  'private_mount_namespace': True, 'production_paths_modified': False,
                  'registered_days': args.day, 'layout': result, 'checked': checked,
                  'historical_continuity': continuity}
        output = BASE / ('private-layout-rehearsal-' + '-'.join(day[-2:] for day in args.day) + '.json')
        with output.open('x') as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        # Drop this namespace's mounts before removing its private scratch tree.
        for day in reversed(args.day):
            run('umount', str(helper.RAW / day))
        for row in reversed(release['changes']):
            if row['path'] != '/usr/local/lib/aurora-radar-retention-layout.py':
                run('umount', row['path'])
        for name, fd in original_controls.items():
            run('umount', '/etc/aurora/' + name)
            os.close(fd)
        run('umount', '/etc/aurora')
        print(json.dumps({'private_rehearsal_passed': args.day, 'proof': str(output)}), flush=True)


if __name__ == '__main__':
    main()
