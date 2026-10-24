import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ble_continuity_env", ROOT / "scripts/blesensors_continuity_environment.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class ContinuityEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = self.root / "dashboard.env"
        self.state = self.root / "state/environment.json"
        self.original = "# existing deployment\nOTHER_SETTING=preserve\nBLESENSORS_DIR=/original/raw\nBLESENSORS_ZARR_PATH=/original/store"
        self.env.write_text(self.original)
        self.env.chmod(0o640)
        self.values = {
            "BLESENSORS_DIR": "/data/aurora/dev-products/blesensors-continuity/raw",
            "BLESENSORS_ZARR_PATH": "/data/aurora/dev-products/blesensors-continuity/blesensors.zarr",
            "BLESENSORS_SOURCE_LABEL": "SwitchBot via ASS - direct-field continuity",
        }

    def configure(self, action="install", check=False):
        return helper.configure(self.env, self.state, action, self.values, check)

    def test_preview_changes_no_files_or_backup_state(self):
        self.assertTrue(self.configure(check=True)["changed"])
        self.assertEqual(self.env.read_text(), self.original)
        self.assertFalse(self.state.parent.exists())

    def test_activation_backup_idempotence_and_selective_rollback(self):
        result = self.configure()
        self.assertEqual((Path(result["backup"]) / self.env.name).read_text(), self.original)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o640)
        self.assertFalse(self.configure()["changed"])
        active = self.env.read_text()
        self.env.write_text(active.replace("OTHER_SETTING=preserve", "OTHER_SETTING=new-live-value") + "LATER_SETTING=keep\n")
        result = self.configure("rollback")
        self.assertTrue(result["changed"])
        restored = self.env.read_text()
        self.assertIn("OTHER_SETTING=new-live-value\n", restored)
        self.assertIn("LATER_SETTING=keep\n", restored)
        self.assertEqual(helper.bindings(restored), helper.bindings(self.original))
        self.assertNotIn("BLESENSORS_SOURCE_LABEL=", restored)
        self.assertFalse(self.configure("rollback")["changed"])

    def test_rollback_refuses_to_overwrite_a_later_ble_binding(self):
        self.configure()
        self.env.write_text(self.env.read_text().replace(self.values["BLESENSORS_ZARR_PATH"], "/new/authoritative/product.zarr"))
        before = self.env.read_bytes()
        with self.assertRaisesRegex(ValueError, "will not overwrite"):
            self.configure("rollback")
        self.assertEqual(self.env.read_bytes(), before)

    def test_missing_baseline_or_injected_setting_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "No saved"):
            self.configure("rollback")
        self.values["BLESENSORS_SOURCE_LABEL"] = "continuity\nOTHER_SETTING=overwritten"
        with self.assertRaisesRegex(ValueError, "single-line"):
            self.configure()
        self.assertEqual(self.env.read_text(), self.original)

    def test_interrupted_activation_can_resume_or_roll_back(self):
        self.configure()
        # Model interruption after saving the baseline but before env replacement.
        self.env.write_text(self.original)
        self.assertTrue(self.configure()["changed"])
        self.assertEqual(helper.bindings(self.env.read_text())["BLESENSORS_DIR"], ["BLESENSORS_DIR=" + self.values["BLESENSORS_DIR"] + "\n"])


class ContinuityBoundaryTests(unittest.TestCase):
    def test_service_is_sequential_isolated_and_has_no_shared_environment(self):
        source = (ROOT / "roles/blesensors_continuity/templates/aurora-blesensors-continuity.service.j2").read_text()
        self.assertEqual(source.count("ExecStart="), 2)
        self.assertLess(source.index("continuity-sync"), source.index("append_new_blesensors"))
        self.assertNotIn("EnvironmentFile=", source)
        self.assertNotIn("aurora-live-continuity", source)
        tasks = yaml.safe_load((ROOT / "roles/blesensors_continuity/tasks/main.yml").read_text())
        systemd_tasks = [task["ansible.builtin.systemd"] for task in tasks if "ansible.builtin.systemd" in task]
        self.assertEqual(len(systemd_tasks), 3)

    def test_continuity_render_removes_archive_dispatch_entirely(self):
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        environment.filters.update(quote=shlex.quote, bool=bool)
        values = {
            "blesensors_source_user": "aurora", "blesensors_source_host": "ass",
            "blesensors_source_port": 22, "blesensors_source_path": "/home/aurora/data/blesensors",
            "blesensors_source_destination": "/data/aurora/dev-products/blesensors-continuity/raw",
            "blesensors_source_state_file": "/data/aurora/dev-products/blesensors-continuity/state/sync.last",
            "blesensors_source_auth": "tailscale", "blesensors_source_ssh_key_path": "unused",
            "blesensors_source_start_fresh": False, "blesensors_source_lookback_days": 10,
            "blesensors_source_mtime_grace_seconds": 300, "blesensors_source_connect_timeout_seconds": 15,
            "blesensors_source_server_alive_interval_seconds": 15, "blesensors_source_server_alive_count_max": 2,
            "aurora_service_user": "aurora", "blesensors_source_patterns": ["*.csv", "ble_temperatures_*.log"],
            "blesensors_source_archive_enqueue": False,
        }
        source = environment.from_string((ROOT / "roles/source_sync/templates/aurora-blesensors-sync.j2").read_text()).render(**values)
        self.assertNotIn("archive-dispatch", source)
        self.assertNotIn("/project/aurora/raw", source)
        self.assertNotIn("--delete", source)
        self.assertNotIn("--remove-source-files", source)
        self.assertIn("ConnectionAttempts=1", source)
        self.assertIn("--timeout=60", source)
        subprocess.run(["bash", "-n"], input=source, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
