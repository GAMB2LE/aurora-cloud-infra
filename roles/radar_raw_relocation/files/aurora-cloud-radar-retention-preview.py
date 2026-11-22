#!/usr/bin/env python3
"""Read-only, non-binding preview of the bounded radar retirement policy."""
import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))


def controller():
    path = Path(__file__).with_name('aurora-cloud-radar-retention.py')
    if not path.exists():
        path = Path(__file__).resolve().with_name('aurora-cloud-radar-retention.py')
    spec = importlib.util.spec_from_file_location('retention_preview_controller', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def build_plan(control, days, maximum):
    if not 1 <= maximum <= 2 or len(days) != len(set(days)) or set(days) - control.DAYS:
        raise ValueError('choose unique reviewed days and a one- or two-day limit')
    helper = control.helper(); routes, pin = helper.routes()
    report = {'schema': 'radar-retention-preview-v1', 'dry_run': True,
              'deletion_authorized': False, 'non_binding': True,
              'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'maximum_days': maximum, 'completed_days_skipped': [], 'candidates': []}
    try:
        control.raw_gate(); report['raw_gate_ready'] = True
    except (ValueError, OSError) as error:
        report.update(raw_gate_ready=False, raw_gate_reason=str(error))
    for day in days:
        stem = day.replace('/', '-'); directory = control.ORIGINALS / day
        result = control.STATE / (stem + '-result.json')
        if day in routes and result.exists():
            helper.guard(day, routes[day])
            if any(directory.iterdir()):
                raise ValueError('a retired staging day was refilled: ' + day)
            report['completed_days_skipped'].append(day); continue
        source_raw = helper.control(control.ARTIFACTS / (stem + '-source-readback.json'))
        historical_raw = helper.control(control.PROOFS / (stem + '.json'))
        source, historical = json.loads(source_raw), json.loads(historical_raw)
        files = source.get('files', {})
        if (source.get('day') != day or historical.get('day') != day or len(files) != 96
                or set(files) != set(historical.get('files', {}))
                or source.get('proof_sha256') != control.sha(historical_raw)
                or any(row['sha256'] != historical['files'][name]['sha256']
                       or row['size'] != historical['files'][name]['size'] for name, row in files.items())):
            raise ValueError('source proof does not describe the reviewed historical day')
        if directory.resolve() != directory or os.path.ismount(directory):
            raise ValueError('unexpected superseded source directory')
        present = {p.name for p in directory.iterdir()}; blockers = []
        if present != set(files): blockers.append('source coverage differs; reconcile before retirement')
        if day in routes: blockers.append('unfinished registered retirement requires reconciliation')
        candidates = []
        for name, expected in sorted(files.items()):
            if Path(name).name != name: raise ValueError('unsafe candidate filename')
            path = directory / name
            if name not in present: continue
            info = path.lstat()
            matches = stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and control.signature(info) == expected['signature']
            if not matches: blockers.append('source version differs: ' + name)
            candidates.append({'path': str(path), 'sha256': expected['sha256'],
                               'expected_signature': expected['signature'],
                               'current_version_matches': matches, 'allocated_bytes': info.st_blocks * 512})
        try:
            control.archive_evidence(day, dt.datetime.now(dt.timezone.utc)); archive_ready = True; archive_reason = None
        except (OSError, ValueError) as error:
            archive_ready = False; archive_reason = str(error)
        folder = control.IMAGES / stem; image = folder / 'archive.btrfs'; receipt = folder / 'verified.json'
        image_ready = False; image_allocated = None
        if image.is_file() and receipt.exists():
            if image.resolve() != image: raise ValueError('unsafe image path')
            record = json.loads(helper.control(receipt)); image_allocated = image.stat().st_blocks * 512
            image_ready = (record.get('day') == day and record.get('source_readback_sha256') == control.sha(source_raw)
                           and record.get('relocation_proof_sha256') == control.sha(historical_raw)
                           and record.get('files') == historical.get('files')
                           and record.get('full_content_and_file_metadata_readback') is True
                           and record.get('image_bytes') == image.stat().st_size)
            if not image_ready: blockers.append('existing image receipt differs; reconcile before retirement')
        elif folder.exists():
            blockers.append('existing image directory is incomplete; reconcile it')
        payload = sum(row['size'] for row in files.values())
        allocated = sum(row['allocated_bytes'] for row in candidates)
        required = 0 if image_ready else payload + 55 * 2**30
        if required and control.available() < required:
            blockers.append('insufficient worst-case staging space and reserve')
        report['candidates'].append({'day': day, 'files': candidates,
            'source_logical_bytes': payload, 'source_allocated_bytes': allocated,
            'existing_image_allocated_bytes': image_allocated,
            'net_bytes_after_existing_image': allocated - image_allocated if image_ready else None,
            'archive_readbacks_current': archive_ready, 'archive_reason': archive_reason,
            'image_verified_receipt_present': image_ready, 'additional_staging_requirement_bytes': required,
            'blockers': blockers, 'preparation_required': {
                'stage_and_fully_verify_image': not image_ready, 'dual_archive_readback': not archive_ready},
            'mandatory_before_any_unlink': ['fresh raw and mirror gates', 'full image hash and immutable seal',
                'drained admissions and exclusive lease', 'accepted 640-file live continuity and science-account layout',
                'exact version journal and no open originals', 'measured space and post-retirement continuity']})
        if len(report['candidates']) >= maximum: break
    if helper.routes() != (routes, pin):
        raise ValueError('registry changed during preview; rerun after the active job finishes')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', action='append')
    parser.add_argument('--max-days', type=int, default=1)
    args = parser.parse_args()
    if os.geteuid() != 0: raise PermissionError('root proof-read access required')
    props = dict(line.split('=', 1) for line in subprocess.check_output(
        ['systemctl', 'show', 'aurora-cloud-radar-retention.service', '-p', 'ActiveState', '-p', 'MainPID'],
        text=True, timeout=15).splitlines() if '=' in line)
    if props.get('ActiveState') in ('active', 'activating', 'deactivating') and props.get('MainPID') != '0':
        print(json.dumps({'schema': 'radar-retention-preview-v1', 'dry_run': True, 'deletion_authorized': False,
                          'status': 'busy', 'reason': 'retirement worker is live; no snapshot or action attempted'})); return
    control = controller()
    print(json.dumps(build_plan(control, args.day or sorted(control.DAYS), args.max_days), indent=2))


if __name__ == '__main__':
    main()
