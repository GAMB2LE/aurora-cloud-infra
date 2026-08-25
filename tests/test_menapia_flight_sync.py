from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest


SCRIPT = (
    Path(__file__).parents[1]
    / "roles"
    / "source_sync"
    / "files"
    / "aurora-menapia-flight-sync.py"
)
TASKS = SCRIPT.parents[1] / "tasks/main.yml"
SERVICE = SCRIPT.parents[1] / "templates/aurora-menapia-flight-source-sync.service.j2"
TIMER = SCRIPT.parents[1] / "templates/aurora-menapia-flight-source-sync.timer.j2"
CONFIG_TEMPLATE = SCRIPT.parents[1] / "templates/menapia-flight-config.json.j2"
INVENTORY = SCRIPT.parents[3] / "inventory/group_vars/aurora_cloud.yml"
SPEC = importlib.util.spec_from_file_location("menapia_flight_sync", SCRIPT)
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def entry(key: str, payload: bytes, *, modified: str = "2026-08-24T09:00:00Z") -> dict:
    return {
        "Path": key,
        "Name": Path(key).name,
        "Size": len(payload),
        "ModTime": modified,
        "IsDir": False,
        "Hashes": {"MD5": f"etag-{len(payload)}"},
        "Metadata": {"content-type": "application/octet-stream"},
    }


class FakeRunner:
    def __init__(self, inventory: list[dict], objects: dict[str, bytes]):
        self.inventory = inventory
        self.objects = objects
        self.download_failures: dict[str, str] = {}
        self.list_failure = ""
        self.archive_failure = ""
        self.metadata_unsupported = False
        self.calls: list[list[str]] = []

    def __call__(self, command, **_kwargs):
        command = [str(value) for value in command]
        self.calls.append(command)
        if (
            self.metadata_unsupported
            and len(command) > 1
            and command[1] in {"lsjson", "copyto"}
            and "--metadata" in command
        ):
            return subprocess.CompletedProcess(
                command, 1, "", "Fatal error: unknown flag: --metadata"
            )
        if len(command) > 1 and command[1] == "lsjson":
            if self.list_failure:
                return subprocess.CompletedProcess(command, 1, "", self.list_failure)
            return subprocess.CompletedProcess(command, 0, json.dumps(self.inventory), "")
        if len(command) > 1 and command[1] == "copyto":
            prefix = "menapia:menapia-flight-data-corrected/"
            assert command[2].startswith(prefix)
            key = command[2][len(prefix) :]
            if key in self.download_failures:
                return subprocess.CompletedProcess(
                    command, 1, "", self.download_failures[key]
                )
            destination = Path(command[3])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self.objects[key])
            return subprocess.CompletedProcess(command, 0, "", "")
        if command and command[0].endswith("archive-dispatch"):
            return subprocess.CompletedProcess(
                command,
                1 if self.archive_failure else 0,
                "",
                self.archive_failure,
            )
        raise AssertionError(f"Unexpected command: {command}")

    @property
    def copy_calls(self) -> list[list[str]]:
        return [call for call in self.calls if len(call) > 1 and call[1] == "copyto"]


class MenapiaFlightSyncTests(unittest.TestCase):
    def setup_case(self, root: Path, inventory: list[dict], objects: dict[str, bytes]):
        raw = root / "raw/menapia"
        dispatcher = root / "bin/aurora-archive-dispatch"
        dispatcher.parent.mkdir(parents=True)
        dispatcher.touch()
        credential = root / "credential/rclone.conf"
        credential.parent.mkdir(parents=True)
        credential.write_text("[menapia]\ntype = s3\n", encoding="utf-8")
        config = {
            "source_remote": "menapia",
            "source_bucket": "menapia-flight-data-corrected",
            "source_region": "eu-west-1",
            "raw_root": str(raw),
            "state_database": str(root / "state/ingest.sqlite3"),
            "status_path": str(root / "internal/status.json"),
            "manifest_root": str(root / "internal/manifests"),
            "lock_path": str(root / "state/ingest.lock"),
            "credential_expires_on": "2026-09-30",
            "rclone_binary": "/usr/bin/rclone",
            "archive_dispatch_command": str(dispatcher),
            "archive_dispatch_required": True,
            "max_objects_per_run": 500,
            "classification": {"dock_ids": {}, "flight_ids": {}},
        }
        return config, credential, FakeRunner(inventory, objects)

    def test_new_object_is_immutable_provenanced_and_enqueued(self):
        key = "drone-uploads/2026/08/24/dock-1/flight-7/data.bin"
        payload = b"flight-data"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, credential, runner = self.setup_case(
                root, [entry(key, payload)], {key: payload}
            )
            result = sync.run_sync(config, credential, runner=runner)

            landed = Path(config["raw_root"]) / key
            status = json.loads(Path(config["status_path"]).read_text())
            manifests = list(Path(config["manifest_root"]).glob("*.jsonl"))
            manifest_rows = [json.loads(line) for line in manifests[0].read_text().splitlines()]
            landed_bytes = landed.read_bytes()
            landed_sha256 = sync.sha256_file(landed)

        self.assertEqual(result, 0)
        self.assertEqual(landed_bytes, payload)
        self.assertEqual(status["state"], "success")
        self.assertEqual(status["new_objects_ingested"], 1)
        self.assertEqual(status["archive_paths_enqueued"], 1)
        self.assertEqual(status["latest_source_flight"]["flight"], "flight-7")
        object_record = next(row for row in manifest_rows if row["record_type"] == "object")
        self.assertEqual(object_record["source_provider"], "Menapia Ltd")
        self.assertEqual(object_record["source_object_key"], key)
        self.assertEqual(object_record["campaign_classification"], "unknown")
        self.assertEqual(object_record["local_sha256"], landed_sha256)

        verbs = [call[1] for call in runner.calls if call[0] == "/usr/bin/rclone"]
        self.assertEqual(verbs, ["lsjson", "copyto"])
        self.assertFalse({"delete", "deletefile", "move", "moveto", "purge", "sync"} & set(verbs))
        for call in (item for item in runner.calls if item[0] == "/usr/bin/rclone"):
            self.assertIn("--s3-region=eu-west-1", call)
        self.assertEqual(
            object_record["source_metadata"],
            {"content-type": "application/octet-stream"},
        )

    def test_repeated_sync_is_idempotent(self):
        key = "drone-uploads/2026/08/24/dock-1/flight-7/data.bin"
        payload = b"same"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, credential, runner = self.setup_case(
                root, [entry(key, payload)], {key: payload}
            )
            self.assertEqual(sync.run_sync(config, credential, runner=runner), 0)
            first_copy_count = len(runner.copy_calls)
            self.assertEqual(sync.run_sync(config, credential, runner=runner), 0)
            status = json.loads(Path(config["status_path"]).read_text())

        self.assertEqual(first_copy_count, 1)
        self.assertEqual(len(runner.copy_calls), 1)
        self.assertEqual(status["new_objects_ingested"], 0)
        self.assertEqual(status["unchanged_objects"], 1)

    def test_interrupted_download_leaves_no_partial_and_recovers(self):
        key = "drone-uploads/2026/08/24/dock-2/flight-8/data.bin"
        payload = b"recoverable"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, credential, runner = self.setup_case(
                root, [entry(key, payload)], {key: payload}
            )
            runner.download_failures[key] = "connection reset"
            self.assertEqual(sync.run_sync(config, credential, runner=runner), 1)
            self.assertFalse((Path(config["raw_root"]) / key).exists())
            self.assertEqual(list(Path(config["raw_root"]).rglob("*.partial-*")), [])

            runner.download_failures.clear()
            self.assertEqual(sync.run_sync(config, credential, runner=runner), 0)
            self.assertEqual((Path(config["raw_root"]) / key).read_bytes(), payload)

    def test_authentication_failure_is_redacted_and_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, credential, runner = self.setup_case(root, [], {})
            access_key = "AKIA" + "A" * 16
            secret = "B" * 40
            runner.list_failure = f"InvalidAccessKeyId {access_key} secret {secret}"
            result = sync.run_sync(config, credential, runner=runner)
            status_text = Path(config["status_path"]).read_text()
            status = json.loads(status_text)

        self.assertEqual(result, 1)
        self.assertTrue(status["authentication_failure"])
        self.assertEqual(status["state"], "failed")
        self.assertNotIn(access_key, status_text)
        self.assertNotIn(secret, status_text)

    def test_expired_credential_and_inaccessible_bucket_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, credential, runner = self.setup_case(root, [], {})
            config["credential_expires_on"] = "2020-01-01"
            runner.list_failure = "bucket endpoint unavailable"
            result = sync.run_sync(config, credential, runner=runner)
            status = json.loads(Path(config["status_path"]).read_text())

        self.assertEqual(result, 1)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["credential"]["level"], "red")
        self.assertLess(status["credential"]["days_remaining"], 0)
        self.assertFalse(status["authentication_failure"])
        self.assertIn("bucket endpoint unavailable", status["failures"][0])

    def test_matching_preexisting_file_is_adopted_without_duplicate(self):
        key = "drone-uploads/2026/08/24/dock-1/flight-7/data.bin"
        payload = b"already-present"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, credential, runner = self.setup_case(
                root, [entry(key, payload)], {key: payload}
            )
            existing = Path(config["raw_root"]) / key
            existing.parent.mkdir(parents=True)
            existing.write_bytes(payload)
            result = sync.run_sync(config, credential, runner=runner)
            revisions = list(
                (Path(config["raw_root"]) / "_upstream_revisions").rglob("data.bin")
            )
            manifest_rows = [
                json.loads(line)
                for line in next(Path(config["manifest_root"]).glob("*.jsonl"))
                .read_text()
                .splitlines()
            ]
            existing_bytes = existing.read_bytes()

        self.assertEqual(result, 0)
        self.assertEqual(existing_bytes, payload)
        self.assertEqual(revisions, [])
        record = next(row for row in manifest_rows if row["record_type"] == "object")
        self.assertEqual(record["outcome"], "adopted_existing")

    def test_upstream_change_preserves_the_original_and_stores_a_revision(self):
        key = "drone-uploads/2026/08/24/dock-1/flight-7/data.bin"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = b"original"
            config, credential, runner = self.setup_case(
                root, [entry(key, first)], {key: first}
            )
            self.assertEqual(sync.run_sync(config, credential, runner=runner), 0)
            canonical = Path(config["raw_root"]) / key

            revised = b"corrected-version"
            runner.inventory = [entry(key, revised, modified="2026-08-25T09:00:00Z")]
            runner.objects[key] = revised
            self.assertEqual(sync.run_sync(config, credential, runner=runner), 0)
            revisions = list((Path(config["raw_root"]) / "_upstream_revisions").rglob("data.bin"))
            status = json.loads(Path(config["status_path"]).read_text())
            canonical_bytes = canonical.read_bytes()
            revision_bytes = revisions[0].read_bytes()

        self.assertEqual(canonical_bytes, first)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revision_bytes, revised)
        self.assertEqual(status["upstream_revisions_ingested"], 1)

    def test_unrelated_and_unsafe_objects_are_preserved_as_unknown(self):
        key = "../other-site/test-flight.bin"
        payload = b"other-site"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, credential, runner = self.setup_case(
                root, [entry(key, payload)], {key: payload}
            )
            self.assertEqual(sync.run_sync(config, credential, runner=runner), 0)
            quarantined = list((Path(config["raw_root"]) / "_unsafe_keys").rglob("payload"))
            manifest = next(Path(config["manifest_root"]).glob("*.jsonl")).read_text()
            quarantined_bytes = quarantined[0].read_bytes()

        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined_bytes, payload)
        self.assertIn('"campaign_classification": "unknown"', manifest)
        self.assertIn('"quarantined_source_key": true', manifest)

    def test_partial_failure_is_not_reported_as_success(self):
        good = "drone-uploads/2026/08/24/dock-1/good/data.bin"
        bad = "drone-uploads/2026/08/24/dock-1/bad/data.bin"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            objects = {good: b"good", bad: b"bad"}
            config, credential, runner = self.setup_case(
                root,
                [entry(good, objects[good]), entry(bad, objects[bad])],
                objects,
            )
            runner.download_failures[bad] = "upstream unavailable"
            result = sync.run_sync(config, credential, runner=runner)
            status = json.loads(Path(config["status_path"]).read_text())

        self.assertEqual(result, 1)
        self.assertEqual(status["state"], "partial_failure")
        self.assertEqual(status["failure_count"], 1)
        self.assertEqual(status["new_objects_ingested"], 1)
        self.assertIsNone(status["last_success_at"])

    def test_archive_enqueue_failure_is_retried_without_redownloading(self):
        key = "drone-uploads/2026/08/24/dock-1/flight-7/data.bin"
        payload = b"archive-me"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, credential, runner = self.setup_case(
                root, [entry(key, payload)], {key: payload}
            )
            runner.archive_failure = "GWS unavailable"
            self.assertEqual(sync.run_sync(config, credential, runner=runner), 1)
            self.assertEqual(len(runner.copy_calls), 1)

            runner.archive_failure = ""
            self.assertEqual(sync.run_sync(config, credential, runner=runner), 0)
            self.assertEqual(len(runner.copy_calls), 1)
            with sqlite3.connect(config["state_database"]) as connection:
                enqueued = connection.execute(
                    "SELECT archive_enqueued FROM object_versions"
                ).fetchone()[0]

        self.assertEqual(enqueued, 1)

    def test_inventory_only_quantifies_without_downloading(self):
        key = "drone-uploads/2026/08/24/dock-1/flight-7/data.bin"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, credential, runner = self.setup_case(
                root, [entry(key, b"data")], {key: b"data"}
            )
            result = sync.run_sync(
                config, credential, runner=runner, inventory_only=True
            )
            status = json.loads(Path(config["status_path"]).read_text())

        self.assertEqual(result, 0)
        self.assertEqual(status["state"], "inventory_only")
        self.assertEqual(status["candidate_objects"], 1)
        self.assertEqual(status["upstream_bytes_examined"], 4)
        self.assertEqual(status["flight_path_objects"], 1)
        self.assertEqual(status["non_flight_path_objects"], 0)
        self.assertEqual(status["source_path_samples"], [key])
        self.assertEqual(runner.copy_calls, [])

    def test_old_rclone_retries_listing_and_copy_without_metadata_flag(self):
        key = "drone-uploads/2026/08/24/dock-1/flight-7/data.bin"
        payload = b"compatible"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, credential, runner = self.setup_case(
                root, [entry(key, payload)], {key: payload}
            )
            runner.metadata_unsupported = True
            result = sync.run_sync(config, credential, runner=runner)
            status = json.loads(Path(config["status_path"]).read_text())
            landed = (Path(config["raw_root"]) / key).read_bytes()

        self.assertEqual(result, 0)
        self.assertEqual(landed, payload)
        self.assertEqual(status["source_metadata_listing"], "basic_compatibility")
        self.assertTrue(
            any("does not support optional S3 metadata" in warning for warning in status["source_path_warnings"])
        )
        list_calls = [call for call in runner.calls if len(call) > 1 and call[1] == "lsjson"]
        copy_calls = [call for call in runner.calls if len(call) > 1 and call[1] == "copyto"]
        self.assertEqual(len(list_calls), 2)
        self.assertEqual(len(copy_calls), 2)
        self.assertIn("--metadata", list_calls[0])
        self.assertNotIn("--metadata", list_calls[1])
        self.assertIn("--metadata", copy_calls[0])
        self.assertNotIn("--metadata", copy_calls[1])

    def test_ansible_uses_loadcredential_and_commissioning_gate(self):
        service = SERVICE.read_text(encoding="utf-8")
        tasks = TASKS.read_text(encoding="utf-8")
        inventory = INVENTORY.read_text(encoding="utf-8")

        self.assertIn("LoadCredential=menapia-rclone.conf:", service)
        self.assertIn("{{ archive_dispatch_state_root }}", service)
        self.assertNotIn("AWS_ACCESS_KEY", service)
        self.assertIn("no_log: true", tasks)
        self.assertIn("menapia_flight_rclone_credential.stat.mode", tasks)
        self.assertIn(
            '{ path: /etc/aurora-menapia, owner: root, group: "{{ aurora_service_group }}", mode: "0750" }',
            tasks,
        )
        self.assertIn("menapia_flight_credentials_commissioned: true", inventory)
        self.assertIn("OnUnitActiveSec={{ menapia_flight_source_sync_interval }}", TIMER.read_text())

    def test_storage_roots_and_internal_manifests_follow_existing_layout(self):
        config = CONFIG_TEMPLATE.read_text(encoding="utf-8")
        inventory = INVENTORY.read_text(encoding="utf-8")

        self.assertIn('"raw_root": {{ aurora_paths.raw_menapia | to_json }}', config)
        self.assertIn('menapia_flight_manifest_root: "{{ aurora_data_root }}/internal/', inventory)
        self.assertIn('destination: "{{ gws_internal_root }}/menapia-flight/manifests/"', inventory)
        self.assertIn("destination: data/internal/aurora-cloud/menapia-flight/manifests", inventory)


if __name__ == "__main__":
    unittest.main()
