import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/register_blesensors_archive.py"
spec = importlib.util.spec_from_file_location("ble_registration", HELPER)
registration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registration)


class ArchiveRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive_stream = {"name": "blesensors", "site": "ass", "prune_enabled": False}
        self.verifier_stream = {"name": "blesensors", "enabled": True, "source_root": "/ble"}
        self.paths = {
            "verifier": self.root / "usr/local/bin/aurora-mirror-verify",
            "health": self.root / "usr/local/bin/aurora-archive-health",
            "catalog": self.root / "etc/aurora-object-store/catalog.json",
            "gws": self.root / "usr/local/bin/aurora-gws-rsync-products",
            "object": self.root / "usr/local/bin/aurora-object-store-copy-products",
        }
        config = {"streams": [{"name": "pdu", "retain_live_customization": 42}], "deployed_setting": "unchanged",
                  "jobs": [{"name": "raw", "exclude": ["keep-raw-policy"]},
                           {"name": "products", "source": "/data/aurora/products/", "exclude": ["wxcam/**"], "settle_age": "20m"}]}
        for path in self.paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        self.paths["verifier"].write_text("# Deployed fix: é\nimport json\nCONFIG = json.loads(" + repr(json.dumps(config)) + ")\nDEPLOYED_FIX = 42\n")
        self.paths["health"].write_text("import json\nSTREAMS = json.loads(" + repr(json.dumps(config["streams"])) + ")\nRECOVERY_ENABLED = True\n")
        self.paths["catalog"].write_text(json.dumps(config))
        self.paths["gws"].write_text('#!/bin/bash\njob_name=products\nsource_dir=/data/aurora/products/\nrsync_opts=(\n  -aH\n)\nrsync "${rsync_opts[@]}"\n')
        self.paths["object"].write_text('#!/bin/bash\njob=products\nsource_path=/data/aurora/products/\nbase_args=(\n  copy "$source_path" "$destination"\n)\nrclone "${base_args[@]}"\n')
        self.paths["verifier"].chmod(0o751)
        self.before = {name: path.read_bytes() for name, path in self.paths.items()}

    def run_registration(self, check=False):
        return registration.register(self.root, self.archive_stream, self.verifier_stream, check)

    def test_check_is_read_only_and_reports_all_bindings(self):
        result = self.run_registration(check=True)
        self.assertTrue(result["changed"])
        self.assertEqual(len(result["paths"]), 5)
        self.assertFalse((self.root / "var").exists())
        for name, path in self.paths.items():
            self.assertEqual(path.read_bytes(), self.before[name])

    def test_register_preserves_live_settings_code_metadata_and_is_idempotent(self):
        result = self.run_registration()
        self.assertTrue(result["changed"])
        backup = Path(result["backup"])
        for name, path in self.paths.items():
            self.assertEqual((backup / path.name).read_bytes(), self.before[name])
        namespace = {}
        exec(self.paths["verifier"].read_text(), namespace)
        self.assertEqual(namespace["DEPLOYED_FIX"], 42)
        self.assertEqual(namespace["CONFIG"]["deployed_setting"], "unchanged")
        self.assertEqual(namespace["CONFIG"]["streams"][0], {"name": "pdu", "retain_live_customization": 42})
        self.assertEqual(namespace["CONFIG"]["streams"][1], self.verifier_stream)
        self.assertEqual(stat.S_IMODE(self.paths["verifier"].stat().st_mode), 0o751)
        self.assertIn("RECOVERY_ENABLED = True", self.paths["health"].read_text())
        catalog = json.loads(self.paths["catalog"].read_text())
        self.assertEqual(catalog["jobs"][0], {"name": "raw", "exclude": ["keep-raw-policy"]})
        self.assertEqual(catalog["jobs"][1]["settle_age"], "20m")
        self.assertEqual(catalog["jobs"][1]["exclude"], ["wxcam/**"] + registration.BLE_PRODUCT_EXCLUDES)
        self.assertFalse(self.run_registration()["changed"])

    def test_unknown_deployed_products_wrapper_prevents_all_writes(self):
        self.paths["object"].write_text('#!/bin/bash\nexec another-copy-worker\n')
        with self.assertRaisesRegex(ValueError, "Unrecognised"):
            self.run_registration()
        for name in ("verifier", "health", "catalog", "gws"):
            self.assertEqual(self.paths[name].read_bytes(), self.before[name])
        self.assertFalse((self.root / "var").exists())

    def test_idle_boundary_rejects_running_or_pending_product_workers(self):
        units = ["aurora-gws-rsync-products.service", "aurora-object-store-copy-products.service",
                 "aurora-object-store-recovery-worker@products.service", "aurora-object-store-inventory-incremental@products.service"]
        def output(state="inactive", job="0"):
            return "\n\n".join(f"Id={name}\nLoadState=loaded\nActiveState={state if i == 0 else 'inactive'}\nJob={job if i == 0 else '0'}" for i, name in enumerate(units))
        for state, job in (("activating", "0"), ("inactive", "123")):
            with mock.patch.object(registration.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output(state, job), "")):
                with self.assertRaisesRegex(ValueError, "idle boundary"):
                    registration.require_idle_product_workers()
        with mock.patch.object(registration.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output(), "")):
            registration.require_idle_product_workers()

    def test_malformed_last_target_prevents_all_writes_and_backups(self):
        self.paths["catalog"].write_text("broken json")
        with self.assertRaises(ValueError):
            self.run_registration()
        for name in ("health", "verifier"):
            self.assertEqual(self.paths[name].read_bytes(), self.before[name])
        self.assertFalse((self.root / "var").exists())

    def test_pruning_cannot_be_enabled_by_focused_registration(self):
        self.archive_stream["prune_enabled"] = True
        with self.assertRaisesRegex(ValueError, "pruning disabled"):
            self.run_registration()

    def test_unknown_deployed_code_binding_fails_closed(self):
        self.paths["verifier"].write_text("CONFIG = load_live_configuration()\n")
        with self.assertRaisesRegex(ValueError, "Unrecognised"):
            self.run_registration()
        self.assertFalse((self.root / "var").exists())


class SourceSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.destination = self.root / "raw"
        self.source.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "state/last"
        self.fixture_names = ["ble_20261023.csv", "ble_temperatures_20261022.log"]
        for name in self.fixture_names:
            (self.source / name).write_text("first record\n")
        # This transport stub emits the selected filenames and records the
        # rendered remote shell for independent selection-policy assertions.
        ssh = self.bin / "ssh"
        ssh.write_text("#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\nPath(os.environ['REMOTE_COMMAND']).write_text(sys.stdin.read())\nif os.getenv('FAIL_SSH'): sys.exit(255)\nsys.stdout.buffer.write(b'ble_20261023.csv\\0ble_temperatures_20261022.log\\0')\n")
        rsync = self.bin / "rsync"
        rsync.write_text("#!/usr/bin/env python3\nimport os,sys,shutil\nfrom pathlib import Path\nif os.getenv('FAIL_RSYNC'): sys.exit(23)\nargs=sys.argv[1:]\nlisting=next(x.split('=',1)[1] for x in args if x.startswith('--files-from='))\nsource=Path(args[-2].split(':',1)[1]); target=Path(args[-1])\nfor name in Path(listing).read_bytes().split(b'\\0'):\n if name: shutil.copy2(source/name.decode(),target/name.decode())\n")
        ssh.chmod(0o755)
        rsync.chmod(0o755)
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        environment.filters.update(quote=shlex.quote, bool=bool)
        variables = {
            "blesensors_source_user": "aurora", "blesensors_source_host": "ass",
            "blesensors_source_port": 22, "blesensors_source_path": str(self.source),
            "blesensors_source_destination": str(self.destination),
            "blesensors_source_state_file": str(self.state), "blesensors_source_auth": "tailscale",
            "blesensors_source_ssh_key_path": "unused", "blesensors_source_start_fresh": False,
            "blesensors_source_lookback_days": 10, "blesensors_source_mtime_grace_seconds": 300,
            "blesensors_source_connect_timeout_seconds": 15, "blesensors_source_server_alive_interval_seconds": 15,
            "blesensors_source_server_alive_count_max": 2, "aurora_service_user": "aurora",
            "blesensors_source_patterns": ["*.csv", "ble_temperatures_*.log"],
        }
        template = (ROOT / "roles/source_sync/templates/aurora-blesensors-sync.j2").read_text()
        self.rendered = environment.from_string(template).render(**variables)
        self.rendered = self.rendered.replace("known_hosts=/home/aurora/.ssh/known_hosts", "known_hosts=" + shlex.quote(str(self.root / "known_hosts")))
        self.script = self.root / "sync.sh"
        self.script.write_text(self.rendered)
        self.environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], REMOTE_COMMAND=str(self.root / "remote.sh"))

    def run_sync(self, **extra):
        return subprocess.run(["bash", str(self.script)], env=dict(self.environment, **extra), capture_output=True, text=True)

    def test_initial_history_replay_and_active_file_growth_are_additive(self):
        subprocess.run(["bash", "-n", str(self.script)], check=True)
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.destination / "older-retained.csv").write_text("preserved")
        for name in self.fixture_names:
            self.assertEqual((self.destination / name).read_bytes(), (self.source / name).read_bytes())
        (self.source / self.fixture_names[0]).write_text("first record\nsecond record\n")
        self.assertEqual(self.run_sync().returncode, 0)
        self.assertEqual((self.destination / self.fixture_names[0]).read_text(), "first record\nsecond record\n")
        self.assertTrue((self.destination / "older-retained.csv").exists())
        self.assertEqual(self.run_sync().returncode, 0)
        self.assertEqual((self.destination / self.fixture_names[0]).read_text().count("second record"), 1)

    def test_failed_transport_does_not_advance_checkpoint(self):
        self.state.parent.mkdir()
        self.state.write_text("12345\n")
        for failure in ("FAIL_SSH", "FAIL_RSYNC"):
            result = self.run_sync(**{failure: "1"})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.state.read_text(), "12345\n")

    def test_selection_and_transport_are_bounded(self):
        self.assertEqual(self.run_sync().returncode, 0)
        remote = (self.root / "remote.sh").read_text()
        self.assertIn("-name '*.csv' -o -name 'ble_temperatures_*.log'", remote)
        self.assertIn('-newermt "@${scan_epoch}"', remote)
        self.assertIn("--timeout=60", self.rendered)
        self.assertIn("ConnectionAttempts=1", self.rendered)
        self.assertNotIn("--delete", self.rendered)
        self.assertNotIn("--remove-source-files", self.rendered)


class InventoryTests(unittest.TestCase):
    def test_new_stream_has_explicit_non_pruning_policy(self):
        variables = yaml.safe_load((ROOT / "inventory/group_vars/aurora_cloud.yml").read_text())
        stream, = [x for x in variables["aurora_archive_streams"] if x["name"] == "blesensors"]
        self.assertEqual(stream["site"], "ass")
        self.assertIs(stream["prune_enabled"], False)
        self.assertEqual(variables["blesensors_source_path"], "/home/aurora/data/blesensors")
        self.assertEqual(variables["blesensors_source_sync_timer_enabled"], "{{ aurora_writer_timers_enabled }}")


if __name__ == "__main__":
    unittest.main()
