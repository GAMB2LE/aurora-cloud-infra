"""Render the actual callers: one lock for raw, unchanged product transport."""
import json
from pathlib import Path
import shlex
import subprocess

import jinja2
import pytest


ROOT = Path(__file__).parents[1]


def render(path, values):
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
    environment.filters['quote'] = lambda value: shlex.quote(str(value))
    environment.filters['to_json'] = json.dumps
    return environment.from_string((ROOT / path).read_text()).render(values)


@pytest.mark.parametrize('name', ['raw', 'products'])
def test_rendered_wrappers_are_valid_shell_and_only_raw_uses_shared_helper(name):
    script = render('roles/gws_sync/templates/aurora-gws-rsync.sh.j2', dict(
        item=dict(name=name, source='/source tree', destination='/destination',
                  copy_links=True, exclude=['rpgfmcw94/.radar-partials/**', '*.tmp']),
        gws_username='user', gws_ssh_private_key='/key', aurora_service_user='aurora',
        gws_manifest_root='/state/manifests', gws_transfer_hosts=['xfer.example'],
        gws_raw_delivery_lock_root='/state/dispatch/gws-transfer',
        gws_raw_batch_files=256, gws_raw_batch_bytes=67108864,
        gws_raw_delivery_wait_seconds=0,
    ))
    result = subprocess.run(['bash', '-n'], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    if name == 'raw':
        assert '/usr/local/lib/aurora-gws-transfer bulk' in script
        assert '--lock-root /state/dispatch/gws-transfer' in script
        assert '--copy-links' in script
    else:
        assert 'aurora-gws-transfer' not in script
        assert 'rsync "${rsync_opts[@]}"' in script
    assert '--delete' not in script


def test_dispatch_config_uses_the_same_writable_lock_directory():
    state = '/state/dispatch'
    config = json.loads(render('roles/archive_dispatch/templates/config.json.j2', dict(
        archive_dispatch_state_root=state, gws_raw_delivery_lock_root=state + '/gws-transfer',
        gws_raw_delivery_wait_seconds=0, object_store_rclone_config='/config',
        object_store_remote='remote', object_store_bucket='bucket', gws_username='user',
        gws_transfer_hosts=['xfer.example'], archive_dispatch_gws_runtime_dir='/run/gws',
        archive_dispatch_max_files_per_run=512, archive_dispatch_max_bytes_per_run=67108864,
        archive_dispatch_receipt_history_keep=10, archive_dispatch_completed_queue_retention_days=7,
        aurora_raw_root='/source', gws_raw_root='/destination', archive_dispatch_raw_settle_age='2m',
        archive_dispatch_object_transfers=4,
    )))
    assert config['gws_delivery_lock_root'] == state + '/gws-transfer'
    assert config['gws_delivery_wait_seconds'] == 0
    assert set(config['jobs']) == {'raw'}
