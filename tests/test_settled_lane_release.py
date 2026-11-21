import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
import gws_worker_release as release
import recover_cloud_gws_stage as recovery


def test_lane_release_and_its_recovery_template_are_bound(monkeypatch):
    monkeypatch.setenv('RADAR_GWS_WORKER_RELEASE', 'gzip-v5-vm01-lanes')
    lane = release.select_release(True)
    assert lane['id'] == 'gzip-v5-vm01-lanes'
    recovery.verify_resume_template(lane)
    template = Path(recovery.HERE) / lane['resume_template']
    assert hashlib.sha256(template.read_bytes()).hexdigest() == lane['resume_template_sha256']
    assert lane['launcher'] in lane['pins'] and lane['fence_launcher'] in lane['pins']
