import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
VARS = yaml.safe_load((ROOT / "roles/standby_replication/vars/main.yml").read_text())
STAGE = next(item for item in VARS["aurora_standby_product_stages"] if item["name"] == "product-blesensors")


class BleMirrorTests(unittest.TestCase):
    def test_only_committed_product_is_transferred(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source", root / "destination"
            destination.mkdir()
            for name in ("blesensors.zarr/.zmetadata", "blesensors.zarr/time/0", "blesensors.ingest.json", "blesensors.ingest.tmp", "blesensors.lock",
                         ".blesensors.zarr.staging.tmp/time/0", ".blesensors.zarr.previous.tmp/time/0",
                         "blesensors.zarr.staging-123/time/0", "blesensors.zarr.previous-123/time/0"):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            args = ["rsync", "-a"] + ["--exclude=" + pattern for pattern in STAGE["exclude"]] + [str(source) + "/", str(destination) + "/"]
            subprocess.run(args, check=True, capture_output=True)
            copied = {str(path.relative_to(destination)) for path in destination.rglob("*") if path.is_file()}
            self.assertEqual(copied, {"blesensors.zarr/.zmetadata", "blesensors.zarr/time/0"})

    def run_transfer(self, locked=True, status=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            rsync = fake_bin / "rsync"
            rsync.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\nPath(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\nprint('Total transferred file size: 123 bytes')\nsys.exit(int(os.environ['RSYNC_STATUS']))\n")
            rsync.chmod(0o755)
            source = (ROOT / "roles/standby_replication/templates/aurora-standby-pull.j2").read_text()
            function = re.search(r"^replicate_path\(\) \{.*?^\}", source, re.M | re.S).group()
            script = "set -euo pipefail\nmkdir() { :; }\nrsync_common=(-a)\nrsync_path='sudo rsync'\nssh_wrapper=/unused\nsource_user=azimuth\nsource_host=primary\nstate_root=" + shlex.quote(str(root)) + "\n" + function + "\n"
            lock = STAGE["source_lock"] if locked else ""
            script += "replicate_path " + " ".join(map(shlex.quote, ["product-blesensors" if locked else "product-power", "/source/", "/data/aurora/products/blesensors/", lock])) + "\n"
            capture = root / "argv.json"
            result = subprocess.run(["bash"], input=script, text=True, capture_output=True,
                                    env=dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"], CAPTURE=str(capture), RSYNC_STATUS=str(status)))
            self.assertTrue(capture.exists(), result.stderr)
            args = json.loads(capture.read_text())
            fresh = (root / ("product-blesensors.json" if locked else "product-power.json")).exists()
            return result, args, fresh

    def test_ble_remote_sender_holds_the_same_bounded_shared_lock(self):
        result, args, fresh = self.run_transfer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--rsync-path=/usr/bin/flock --shared --timeout 30 /data/aurora/products/blesensors/blesensors.lock sudo rsync", args)
        self.assertTrue(fresh)

    def test_ble_vanished_source_is_not_reported_as_success(self):
        result, _, fresh = self.run_transfer(status=24)
        self.assertEqual(result.returncode, 24)
        self.assertFalse(fresh)

    def test_other_stage_transport_and_vanished_source_policy_are_preserved(self):
        result, args, fresh = self.run_transfer(locked=False, status=24)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--rsync-path=sudo rsync", args)
        self.assertTrue(fresh)

    @unittest.skipUnless(shutil.which("rclone"), "local rclone unavailable")
    def test_archive_tree_filters_exclude_nested_sidecars(self):
        config = yaml.safe_load((ROOT / "inventory/group_vars/aurora_cloud.yml").read_text())
        job = next(item for item in config["object_store_jobs"] if item["name"] == "products")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("blesensors/blesensors.zarr/time/0", "blesensors/.blesensors.zarr.staging.tmp/time/0", "blesensors/.blesensors.zarr.previous.tmp/time/0", "blesensors/blesensors.ingest.json", "blesensors/blesensors.lock"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            args = ["rclone", "lsf", "--recursive", "--files-only", "--config", "/dev/null"]
            for pattern in job["exclude"]:
                args += ["--exclude", pattern]
            result = subprocess.run(args + [str(root)], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(), ["blesensors/blesensors.zarr/time/0"])


if __name__ == "__main__":
    unittest.main()
