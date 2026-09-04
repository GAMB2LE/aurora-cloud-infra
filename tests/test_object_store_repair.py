from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import json


SCRIPT = (
    Path(__file__).parents[1]
    / "roles"
    / "object_store_mirror"
    / "files"
    / "aurora-object-store-repair-from-report.py"
)
SPEC = importlib.util.spec_from_file_location("object_store_repair", SCRIPT)
assert SPEC and SPEC.loader
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)


class ObjectStoreRepairTests(unittest.TestCase):
    def test_publish_result_is_atomic_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "result.json"
            payload = {"report": "now", "jobs": [{"job": "products"}]}

            repair.publish_result(path, payload)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_repair_waits_for_inventory_then_reads_terminal_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_root = root / "manifests"
            latest = manifest_root / "latest"
            latest.mkdir(parents=True)
            lock_path = manifest_root / ".inventory.lock"
            lock_path.touch()
            report_path = latest / "comparison.json"
            report_path.write_text(
                json.dumps(
                    {
                        "generated_at": "checkpoint",
                        "jobs": {"products": {"phase": "checkpoint"}},
                    }
                ),
                encoding="utf-8",
            )
            (latest / "products-local.tsv").write_text(
                "relative_path\tsize\tmtime\tchecksum\n",
                encoding="utf-8",
            )
            result_path = root / "state" / "result.json"
            args = mock.Mock(
                report=report_path,
                result=result_path,
                job=None,
                dry_run=False,
            )
            catalog = {
                "manifest_root": str(manifest_root),
                "jobs": [{"name": "products"}],
            }
            started_waiting = threading.Event()
            finished = threading.Event()
            outcome: dict[str, object] = {}
            original_inventory_lock = repair.inventory_lock
            original_publish_result = repair.publish_result

            def observed_inventory_lock(value: dict):
                started_waiting.set()
                return original_inventory_lock(value)

            def run_repair() -> None:
                try:
                    outcome["payload"] = repair.repair_latest(args, catalog)
                except BaseException as error:  # Preserve worker failures for assertion.
                    outcome["error"] = error
                finally:
                    finished.set()

            def observed_publish_result(path: Path, payload: dict) -> None:
                with lock_path.open("r", encoding="utf-8") as contender:
                    try:
                        repair.fcntl.flock(
                            contender.fileno(),
                            repair.fcntl.LOCK_EX | repair.fcntl.LOCK_NB,
                        )
                    except BlockingIOError:
                        outcome["lock_held_during_publish"] = True
                    else:
                        repair.fcntl.flock(
                            contender.fileno(), repair.fcntl.LOCK_UN
                        )
                original_publish_result(path, payload)

            with lock_path.open("r", encoding="utf-8") as inventory_handle:
                repair.fcntl.flock(inventory_handle.fileno(), repair.fcntl.LOCK_EX)
                with (
                    mock.patch.object(
                        repair,
                        "inventory_lock",
                        side_effect=observed_inventory_lock,
                    ),
                    mock.patch.object(
                        repair,
                        "repair_job",
                        return_value={
                            "job": "products",
                            "candidates": 0,
                            "ready": 0,
                            "deferred": 0,
                            "returncode": 0,
                        },
                    ) as repair_job,
                    mock.patch.object(
                        repair,
                        "publish_result",
                        side_effect=observed_publish_result,
                    ),
                ):
                    worker = threading.Thread(target=run_repair)
                    worker.start()
                    self.assertTrue(started_waiting.wait(timeout=1))
                    self.assertFalse(finished.wait(timeout=0.1))
                    self.assertFalse(result_path.exists())

                    replacement = latest / "comparison.next.json"
                    replacement.write_text(
                        json.dumps(
                            {
                                "generated_at": "terminal",
                                "jobs": {"products": {"phase": "terminal"}},
                            }
                        ),
                        encoding="utf-8",
                    )
                    replacement.replace(report_path)
                    repair.fcntl.flock(
                        inventory_handle.fileno(), repair.fcntl.LOCK_UN
                    )

                    self.assertTrue(finished.wait(timeout=2))
                    worker.join(timeout=1)

            self.assertNotIn("error", outcome)
            self.assertIs(outcome["lock_held_during_publish"], True)
            self.assertEqual(
                outcome["payload"],
                {
                    "report": "terminal",
                    "jobs": [
                        {
                            "job": "products",
                            "candidates": 0,
                            "ready": 0,
                            "deferred": 0,
                            "returncode": 0,
                        }
                    ],
                },
            )
            self.assertEqual(
                json.loads(result_path.read_text(encoding="utf-8")),
                outcome["payload"],
            )
            self.assertEqual(repair_job.call_args.args[2]["phase"], "terminal")

    def test_repair_uses_verification_horizon_not_writer_latency(self) -> None:
        self.assertEqual(
            repair.verification_settle_age(
                {"settle_age": "15m", "verification_settle_age": "6h"}
            ),
            "6h",
        )

    def test_settled_paths_are_newest_first_and_symlinks_are_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = root / "older.nc"
            newer = root / "newer.nc"
            target = root / "target.nc"
            link = root / "link.nc"
            for path in (older, newer, target):
                path.write_bytes(b"x")
            os.utime(older, (100, 100))
            os.utime(newer, (200, 200))
            os.utime(target, (150, 150))
            link.symlink_to(target)

            ready, deferred = repair.settled_paths(
                root,
                {"older.nc", "newer.nc", "link.nc", "../outside.nc"},
                0,
            )

        self.assertEqual(ready, ["newer.nc", "older.nc"])
        self.assertEqual(deferred, ["../outside.nc", "link.nc"])

    def test_settled_paths_include_links_for_dereferenced_archive_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "runtime.dat"
            target.write_bytes(b"input table")
            link = root / "linked.dat"
            link.symlink_to(target.name)

            ready, deferred = repair.settled_paths(
                root,
                {"linked.dat"},
                0,
                copy_links=True,
            )

        self.assertEqual(ready, ["linked.dat"])
        self.assertEqual(deferred, [])

    def test_external_link_requires_matching_immutable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            target = root / "shared-runtime.dat"
            target.write_bytes(b"approved input")
            link = source / "linked.dat"
            link.symlink_to(target)
            stat = link.stat()
            evidence = {
                "linked.dat": {
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime),
                }
            }

            ready, deferred = repair.settled_paths(
                source,
                {"linked.dat"},
                0,
                copy_links=True,
                evidence=evidence,
            )
            target.write_bytes(b"changed after verification")
            changed_ready, changed_deferred = repair.settled_paths(
                source,
                {"linked.dat"},
                0,
                copy_links=True,
                evidence=evidence,
            )

        self.assertEqual(ready, ["linked.dat"])
        self.assertEqual(deferred, [])
        self.assertEqual(changed_ready, [])
        self.assertEqual(changed_deferred, ["linked.dat"])

    def test_local_evidence_is_read_from_report_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "comparison.json"
            report.write_text("{}", encoding="utf-8")
            (root / "model-evaluation-local.tsv").write_text(
                "relative_path\tsize\tmtime\tchecksum\n"
                "linked.dat\t42\t100.0\t\n",
                encoding="utf-8",
            )

            evidence = repair.read_local_evidence(report, "model-evaluation")

        self.assertEqual(
            evidence,
            {"linked.dat": {"size": 42, "mtime": 100}},
        )

    def test_repair_uses_catalogue_and_exact_files_from_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "new.nc").write_bytes(b"new")
            (source / "old.nc").write_bytes(b"old")
            os.utime(source / "new.nc", (200, 200))
            os.utime(source / "old.nc", (100, 100))
            captured: dict[str, object] = {}

            def fake_run(command: list[str], check: bool) -> mock.Mock:
                captured["command"] = command
                list_arg = next(
                    item for item in command if item.startswith("--files-from-raw=")
                )
                captured["paths"] = Path(list_arg.split("=", 1)[1]).read_text(
                    encoding="utf-8"
                ).splitlines()
                return mock.Mock(returncode=0)

            with mock.patch.object(repair.subprocess, "run", side_effect=fake_run):
                result = repair.repair_job(
                    "raw",
                    {
                        "source": str(source),
                        "destination": "data/raw",
                        "settle_age": "0s",
                    },
                    {
                        "source_vs_s3": {
                            "missing_from_right": ["old.nc", "new.nc"],
                            "size_mismatch": [],
                            "checksum_mismatch": [],
                        }
                    },
                    {
                        "remote": "remote",
                        "bucket": "bucket",
                        "rclone_config": "/config",
                    },
                    False,
                )

        self.assertEqual(result["ready"], 2)
        self.assertEqual(captured["paths"], ["new.nc", "old.nc"])
        command = captured["command"]
        assert isinstance(command, list)
        self.assertIn("remote:bucket/data/raw/", command)
        self.assertIn("--ignore-times", command)
        self.assertFalse(any("delete" in item for item in command))

    def test_repair_dereferences_links_when_catalogue_requires_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            target = source / "runtime.dat"
            target.write_bytes(b"input table")
            (source / "linked.dat").symlink_to(target.name)
            captured: dict[str, object] = {}

            def fake_run(command: list[str], check: bool) -> mock.Mock:
                captured["command"] = command
                return mock.Mock(returncode=0)

            with mock.patch.object(repair.subprocess, "run", side_effect=fake_run):
                result = repair.repair_job(
                    "model-evaluation",
                    {
                        "source": str(source),
                        "destination": "data/model",
                        "settle_age": "0s",
                        "copy_links": True,
                    },
                    {
                        "source_vs_s3": {
                            "missing_from_right": ["linked.dat"],
                            "size_mismatch": [],
                            "checksum_mismatch": [],
                        }
                    },
                    {
                        "remote": "remote",
                        "bucket": "bucket",
                        "rclone_config": "/config",
                    },
                    False,
                )

        self.assertEqual(result["ready"], 1)
        command = captured["command"]
        assert isinstance(command, list)
        self.assertIn("--copy-links", command)


if __name__ == "__main__":
    unittest.main()
