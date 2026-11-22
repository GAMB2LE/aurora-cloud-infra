#!/usr/bin/env python3
"""Retire verified uncompressed radar duplicates while retaining exact online views."""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

from radar_retention_maintenance import Maintenance, tools, AdmissionDeferred
from radar_retention_evidence import (ARTIFACTS, ORIGINALS, IMAGES, PROOFS, DAYS,
    archive_evidence, remove_duplicates, save, sha, signature)
from importlib.machinery import SourceFileLoader

STATE = Path('/var/lib/aurora-cloud/radar-product-retention')
REGISTRY = Path('/etc/aurora/radar-retention-compressed.json')
LIBRARY = Path('/usr/local/lib/aurora-radar-retention')


class GateDeferred(ValueError):
    """Await fresh raw confirmations without authorizing duplicate removal."""


def helper():
    spec = importlib.util.spec_from_file_location('retention_layout', '/usr/local/lib/aurora-radar-retention-layout.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def validator():
    module = SourceFileLoader('retention_installer_validation', str(LIBRARY / 'install-retention-layout-release.py')).load_module()
    def check():
        result = module.validate()
        result['science_account_layout'] = json.loads(subprocess.check_output(
            ['sudo', '-n', '-u', 'aurora', '/usr/bin/python3', str(module.LAYOUT)],
            text=True, timeout=180))
        return result
    return check


def await_existing(unit, timeout=21600):
    deadline = time.monotonic() + timeout
    while True:
        text = subprocess.check_output(['systemctl', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState',
                                        '-p', 'MainPID', '-p', 'Result'], text=True, timeout=30)
        state = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        if state.get('LoadState') == 'not-found' or state.get('ActiveState') in ('inactive', 'failed'):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError('existing producer is still live; no duplicate started: ' + unit)
        print(json.dumps({'waiting_for_existing_producer': unit, 'pid': state.get('MainPID')}), flush=True)
        time.sleep(30)


def prepare(day):
    """Produce missing proof outside maintenance; never duplicate live producers."""
    stamp = day[1:5] + day[7:9] + day[11:13]
    image_receipt = IMAGES / day.replace('/', '-') / 'verified.json'
    if not image_receipt.exists():
        await_existing('aurora-retention-recovery-image-' + stamp + '.service')
        if not image_receipt.exists():
            subprocess.run(['/usr/bin/python3', str(LIBRARY / 'stage-retention-radar-image.py'), '--day', day],
                           check=True, timeout=14400)
    try:
        archive_evidence(day, dt.datetime.now(dt.timezone.utc))
    except (FileNotFoundError, ValueError):
        await_existing('aurora-retention-archive-readback-' + stamp + '.service')
        try:
            archive_evidence(day, dt.datetime.now(dt.timezone.utc))
        except (FileNotFoundError, ValueError):
            subprocess.run(['/opt/aurora-object-store-recovery/venv/bin/python',
                str(LIBRARY / 'verify-retention-radar-archives.py'), '--day', day], check=True, timeout=21600)
    archive_evidence(day, dt.datetime.now(dt.timezone.utc))


def raw_gate():
    sys.path.insert(0, '/usr/local/lib/aurora-retention-window')
    from retention_window_gate import require_ready
    module = runpy.run_path('/usr/local/bin/aurora-ass-retention', run_name='cloud_retention_gate')
    summary = json.loads((module['MIRROR'] / 'summary.json').read_text())
    config = {'manifest_root': str(module['OBJECT'].parent.parent), 'gate_state_path': str(module['OBJECT_GATE'])}
    with module['read_snapshot'](config) as snapshot:
        gate, report = snapshot.gate, snapshot.report
        try:
            require_ready(gate, summary, [s['name'] for s in module['STREAMS'] if s.get('prune_enabled')],
                          dt.datetime.now(dt.timezone.utc))
        except ValueError as error:
            raise GateDeferred(str(error)) from error
        if gate.get('last_generated_at') != report.get('generated_at'):
            raise ValueError('raw gate does not describe the pinned inventory')
        raw = report.get('jobs', {}).get('raw', {}).get('source_vs_s3', {})
        if any(raw.get(k) for k in ('missing_from_right', 'size_mismatch', 'checksum_mismatch')):
            raise ValueError('raw archive parity changed')


def available():
    disk = os.statvfs('/data')
    return disk.f_bavail * disk.f_frsize


def image_route(day):
    module = helper(); folder = IMAGES / day.replace('/', '-')
    image, receipt_path = folder / 'archive.btrfs', folder / 'verified.json'
    # The ordinary science account must be able to stat the immutable image and
    # read its nonsecret proof. Grant only traversal and proof-read permissions;
    # image contents and every controller remain protected from writes.
    for directory in (ARTIFACTS, IMAGES, folder):
        if directory.resolve() != directory or directory.stat().st_uid != 0:
            raise ValueError('unsafe verification control directory')
        subprocess.run(['setfacl', '-m', 'u:aurora:--x', str(directory)], check=True, timeout=30)
    subprocess.run(['setfacl', '-m', 'u:aurora:r--', str(receipt_path)], check=True, timeout=30)
    raw = receipt_path.read_bytes(); receipt = json.loads(raw)
    if receipt.get('full_content_and_file_metadata_readback') is not True or receipt.get('day') != day:
        raise ValueError('unverified recovery image')
    before = signature(image.stat())
    content = hashlib.sha256()
    with image.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            content.update(block)
    if content.hexdigest() != receipt['image_sha256'] or signature(image.stat()) != before:
        raise ValueError('sealed recovery image checksum differs')
    existing_flags = subprocess.check_output(['lsattr', '-d', str(image)], text=True, timeout=30).split()[0]
    if 'i' not in existing_flags:
        subprocess.run(['chattr', '+i', str(image)], check=True, timeout=30)
    flags = subprocess.check_output(['lsattr', '-d', str(image)], text=True, timeout=30).split()[0]
    if 'i' not in flags or receipt_path.read_bytes() != raw:
        raise ValueError('recovery image protection or receipt changed')
    return {'image_sha256': receipt['image_sha256'], 'image_identity': module.signature(image.stat()),
            'receipt_sha256': sha(raw), 'relocation_manifest_sha256': receipt['relocation_proof_sha256']}


def mount_unit(day):
    target = helper().RAW / day
    name = subprocess.check_output(['systemd-escape', '--path', '--suffix=mount', str(target)], text=True).strip()
    image = IMAGES / day.replace('/', '-') / 'archive.btrfs'
    text = ('[Unit]\nDescription=Verified compressed online radar day\n'
            'Requires=project.mount data.mount\nAfter=project.mount data.mount\n'
            'BindsTo=project.mount data.mount\n\n[Mount]\n'
            f'What={image}\nWhere={target}\nType=btrfs\n'
            'Options=loop,ro,nologreplay,nodev,nosuid,noexec\nTimeoutSec=90\n\n'
            '[Install]\nWantedBy=multi-user.target\n')
    return name, Path('/etc/systemd/system') / name, text.encode()


def refresh_readers(day, utility):
    target = helper().RAW / day
    expected = f'{target.stat().st_dev}:{target.stat().st_ino}'
    result = {}
    for unit in ('aurora-mobile-api.service', 'aurora-dashboard.service'):
        props = utility.props(unit)
        if props['ActiveState'] != 'active' or int(props['MainPID']) <= 0:
            result[unit] = 'previously-inactive'; continue
        def visible(pid):
            return utility.run(['nsenter', '-t', pid, '-m', 'stat', '-c', '%d:%i', str(target)]).strip()
        if visible(props['MainPID']) != expected:
            utility.run(['systemctl', 'restart', unit], timeout=90)
            props = utility.props(unit)
            if props['ActiveState'] != 'active' or int(props['MainPID']) <= 0 or visible(props['MainPID']) != expected:
                raise ValueError('presentation reader did not acquire the verified new view')
            result[unit] = 'namespace-refreshed'
        else:
            result[unit] = 'current-view-verified'
    return result


def prevent_refill(day, route):
    marker = PROOFS / 'stage-withdrawal.json'
    if not marker.exists():
        record = {'schema': 'radar-compressed-retention-stage-withdrawal-v1',
                  'reason': 'historical cohort now uses verified compressed online views; old staging commands must not refill it',
                  'first_replaced_day': day, 'image_sha256': route['image_sha256'],
                  'created_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        with marker.open('x') as stream:
            json.dump(record, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        fd = os.open(marker.parent, os.O_DIRECTORY); os.fsync(fd); os.close(fd)
    return {'marker': str(marker), 'sha256': sha(marker.read_bytes())}


def retire(day):
    if day not in DAYS:
        raise ValueError('unreviewed radar retention day')
    raw_gate()
    evidence = archive_evidence(day, dt.datetime.now(dt.timezone.utc))
    expected = {name: row['signature'] for name, row in evidence['files'].items()}
    route = image_route(day)
    module = helper(); selected, _ = module.routes()
    if day in selected and selected[day] != route:
        raise ValueError('registered image differs from the sealed recovery image')
    original = ORIGINALS / day
    stem = day.replace('/', '-')
    journal = STATE / (stem + '-duplicates.json')
    result_path = STATE / (stem + '-result.json')
    t = tools()
    with Maintenance('retire-' + stem.lower().replace('/', '-')) as window:
        raw_gate(); archive_evidence(day, dt.datetime.now(dt.timezone.utc))
        module = helper(); selected, _ = module.routes()
        is_new = day not in selected
        if is_new:
            if {p.name for p in original.iterdir()} != set(expected):
                raise ValueError('all original observations must remain before first cutover')
            if any(signature((original / n).lstat()) != row for n, row in expected.items()):
                raise ValueError('original version changed since complete byte readback')
            name, unit_path, new_unit = mount_unit(day)
            previous_unit = t.control(unit_path)
            previous_registry = REGISTRY.read_bytes() if REGISTRY.exists() else None
            save(window.folder / 'cutover-intent.json', {'day': day, 'route': route, 'mount_unit': name,
                'previous_unit_sha256': sha(previous_unit), 'previous_registry_sha256': sha(previous_registry) if previous_registry else None})
            installer = SourceFileLoader('atomic_retention_install', str(LIBRARY / 'install-retention-layout-release.py')).load_module()
            installer.atomic(window.folder / 'mount-unit.original', previous_unit,
                             {'uid': 0, 'gid': 0, 'mode': 0o400})
            if previous_registry is not None:
                installer.atomic(window.folder / 'registry.original', previous_registry,
                                 {'uid': 0, 'gid': 0, 'mode': 0o400})
            window.unsafe()
            try:
                installer.atomic(unit_path, new_unit, {'uid': 0, 'gid': 0, 'mode': 0o644})
                t.run(['systemctl', 'daemon-reload'])
                t.run(['systemctl', 'stop', name], timeout=120)
                t.run(['systemctl', 'start', name], timeout=120)
                module.guard(day, route)
                selected[day] = route
                installer.atomic(REGISTRY, (json.dumps({'schema_version': 1, 'routes': selected}, sort_keys=True) + '\n').encode(),
                                 {'uid': 0, 'gid': 0, 'mode': 0o644})
                window.state['live_verification'] = window.handoff_check(validator())
                window.safe()
            except BaseException:
                window.reenter_exclusive()
                installer.atomic(unit_path, previous_unit, {'uid': 0, 'gid': 0, 'mode': 0o644})
                if previous_registry is None:
                    if REGISTRY.exists(): REGISTRY.unlink(); t.syncdir(REGISTRY.parent)
                else:
                    installer.atomic(REGISTRY, previous_registry, {'uid': 0, 'gid': 0, 'mode': 0o644})
                t.run(['systemctl', 'daemon-reload'])
                t.run(['systemctl', 'stop', name], timeout=120)
                t.run(['systemctl', 'start', name], timeout=120)
                window.state['rollback_verification'] = window.handoff_check(validator())
                window.safe()
                raise
        else:
            module.guard(day, route)
            window.state['live_verification'] = window.handoff_check(validator())
        reader_views = refresh_readers(day, t)
        withdrawal = prevent_refill(day, route)
        before = available()
        raw_gate()
        def check():
            try:
                helper().guard(day, route)
            except BaseException:
                window.unsafe(); window.reenter_exclusive()
                raise
        deleted = remove_duplicates(original, expected, journal, check)
        after = available()
        try:
            post = validator()()
        except BaseException:
            window.reenter_exclusive(); window.unsafe()
            raise
        if len(deleted['deleted']) != 96 or not deleted.get('complete'):
            raise ValueError('not every superseded duplicate has a durable receipt')
        record = {'schema': 'radar-product-retention-result-v1', 'day': day,
                  'verified_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'image_route': route,
                  'archive_evidence': evidence, 'duplicate_journal': str(journal),
                  'allocated_bytes_removed': deleted['allocated_bytes_removed'],
                  'available_bytes_before': before, 'available_bytes_after': after,
                  'observed_recovered_bytes': after - before, 'reader_views': reader_views,
                  'refill_prevention': withdrawal, 'historical_continuity': post,
                  'online_science_files_preserved': 640, 'maintenance_operation': str(window.folder)}
        save(result_path, record)
        window.state['retirement_result'] = str(result_path); window.save()
        print(json.dumps({'retired_duplicate_day': day, 'observed_recovered_bytes': after - before,
                          'science_files_preserved': 640, 'receipt': str(result_path)}), flush=True)
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', action='append')
    parser.add_argument('--max-days', type=int, default=1)
    args = parser.parse_args()
    if os.geteuid() != 0 or not 1 <= args.max_days <= 2:
        raise ValueError('root control and a one- or two-day bound required')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.resolve() != STATE or STATE.stat().st_uid or STATE.stat().st_mode & 0o077:
        raise ValueError('unsafe cloud retention receipt directory')
    import fcntl
    with (STATE / 'run.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        selected, _ = helper().routes()
        completed = 0
        for day in args.day or sorted(DAYS):
            if day not in DAYS: raise ValueError('unreviewed day')
            if day in selected and (STATE / (day.replace('/', '-') + '-result.json')).exists():
                helper().guard(day, selected[day])
                if any((ORIGINALS / day).iterdir()):
                    raise ValueError('retired staging day was refilled')
                continue
            raw_gate(); prepare(day)
            retire(day); completed += 1
            if completed >= args.max_days: break
        print(json.dumps({'cloud_retention_days_completed_this_run': completed}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (GateDeferred, AdmissionDeferred) as error:
        print(json.dumps({'cloud_retention_deferred': str(error), 'duplicate_removal_authorized': False}), file=sys.stderr)
        raise SystemExit(75)
