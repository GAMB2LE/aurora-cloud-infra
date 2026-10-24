"""Exercise deletion and unavailable-mount paths without touching live data."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'roles/standby_replication/templates/aurora-standby-pull.j2'


def run_stage(tmp_path, stage='product-dashboard', *, deleting=True, reconciling=False,
              destination_mounted=True, source_ready=True, status=0, raw_guard=False):
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    capture = tmp_path / 'rsync.json'
    wrappers = {
        'mountpoint': '#!/bin/sh\nexit "$DESTINATION_STATUS"\n',
        'ssh-wrapper': '#!/bin/sh\nexit "$SOURCE_STATUS"\n',
        'rsync': """#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))
print('Total transferred file size: 12 bytes')
sys.exit(int(os.environ['RSYNC_STATUS']))
""",
    }
    for name, content in wrappers.items():
        path = fake_bin / name
        path.write_text(content)
        path.chmod(0o755)
    function = re.search(r'^replicate_path\(\) \{.*?^\}', TEMPLATE.read_text(), re.M | re.S).group()
    for prefix in ('/data/aurora', '/project/aurora', '/var/lib/aurora-cloud'):
        function = function.replace(prefix, str(tmp_path / prefix.lstrip('/')))
    path = (tmp_path / 'project/aurora/raw' / ('rpgfmcw94' if stage == 'raw-radar' else 'power') if stage.startswith('raw-')
            else tmp_path / 'data/aurora/products' / stage.removeprefix('product-'))
    path.mkdir(parents=True)
    orphan = path / 'outage-only.dat'
    orphan.write_text('must remain')
    stamp = tmp_path / (stage + '.json')
    stamp.write_text('previous')
    script = '\n'.join([
        'set -euo pipefail', 'rsync_common=(-a)', "rsync_path='sudo rsync'",
        f'ssh_wrapper={shlex.quote(str(fake_bin / "ssh-wrapper"))}',
        'source_user=azimuth', 'source_host=primary', f'state_root={shlex.quote(str(tmp_path))}',
        f'delete_enabled={str(deleting).lower()}', f'reconciling={str(reconciling).lower()}',
        f'delete_stages={stage}', f'raw_relocation_guard={str(raw_guard).lower()}', function,
        'replicate_path ' + ' '.join(map(shlex.quote, [stage, str(path) + '/', str(path) + '/', ''])),
    ])
    result = subprocess.run(['bash'], input=script, text=True, capture_output=True, timeout=10,
                            env={**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
                                 'CAPTURE': str(capture), 'DESTINATION_STATUS': '0' if destination_mounted else '1',
                                 'SOURCE_STATUS': '0' if source_ready else '1', 'RSYNC_STATUS': str(status)})
    return result, json.loads(capture.read_text()) if capture.exists() else None, stamp.read_text(), orphan.read_text()


def test_reconciliation_never_deletes_even_when_requested(tmp_path):
    result, args, stamp, orphan = run_stage(tmp_path, reconciling=True)
    assert result.returncode == 0, result.stderr
    assert not any(arg.startswith('--delete') for arg in args)
    assert json.loads(stamp)['stage'] == 'product-dashboard'
    assert orphan == 'must remain'


def test_raw_deletion_is_rejected_even_after_reconciliation(tmp_path):
    result, args, stamp, _ = run_stage(tmp_path, stage='raw-power')
    assert result.returncode == 2
    assert args is None
    assert stamp == 'previous'


@pytest.mark.parametrize('destination_mounted,source_ready', [(False, True), (True, False)])
def test_missing_source_or_mount_preserves_destination_and_success(tmp_path, destination_mounted, source_ready):
    result, args, stamp, orphan = run_stage(tmp_path, destination_mounted=destination_mounted, source_ready=source_ready)
    assert result.returncode == 75, result.stderr
    assert args is None
    assert stamp == 'previous'
    assert orphan == 'must remain'


def test_disposable_replica_requires_explicit_opt_in_and_sender_recheck(tmp_path):
    result, args, _, _ = run_stage(tmp_path)
    assert result.returncode == 0, result.stderr
    assert '--delete-delay' in args
    sender = next(arg for arg in args if arg.startswith('--rsync-path='))
    assert 'mountpoint -q /data' in sender
    assert 'find ' in sender and '-mindepth 1 -maxdepth 1' in sender
    assert sender.endswith('&& sudo rsync')


def test_transfer_failure_never_advances_success(tmp_path):
    result, args, stamp, orphan = run_stage(tmp_path, deleting=False, status=23)
    assert result.returncode == 23
    assert args is not None
    assert stamp == 'previous'
    assert orphan == 'must remain'


def test_recovery_inventory_keeps_deletion_and_retention_off():
    group = yaml.safe_load((ROOT / 'inventory/group_vars/aurora_cloud.yml').read_text())
    production = yaml.safe_load((ROOT / 'inventory/host_vars/aurora-cloud.yml').read_text())
    mirror = yaml.safe_load((ROOT / 'inventory/host_vars/aurora-cloud-droplet.yml').read_text())
    assert group['aurora_standby_replication_delete'] is False
    assert group['aurora_standby_replication_reconciling'] is True
    assert group['aurora_standby_replication_delete_stages'] == []
    assert production['ass_retention_timer_enabled'] is False
    assert mirror['aurora_standby_raw_relocation_guard'] is True


def test_radar_sender_holds_relocation_guard_lock_for_entire_transfer(tmp_path):
    result, args, stamp, _ = run_stage(tmp_path, stage='raw-radar', deleting=False, raw_guard=True)
    assert result.returncode == 0, result.stderr
    sender = next(arg for arg in args if arg.startswith('--rsync-path='))
    assert 'aurora-radar-relocation.py exec --if-active' in sender
    assert sender.endswith('-- sudo rsync')  # rsync appends its server arguments here.
    assert json.loads(stamp)['stage'] == 'raw-radar'
