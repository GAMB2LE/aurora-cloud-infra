"""Exercise queue, frozen sources, paged reader and canonical publication together."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

FILES = Path(__file__).parents[1] / "roles/object_store_mirror/files"
sys.path.insert(0, str(FILES))
import aurora_object_store_evidence as evidence
import aurora_object_store_recovery as recovery
import aurora_object_store_s3 as reader

FAMILIES = ("raw", "products", "products-wxcam", "model-evaluation", "menapia-flight-manifests", "manifests")


class GatewayTimeout(Exception):
    response = {"Error": {"Code": "GatewayTimeout"}, "ResponseMetadata": {"HTTPStatusCode": 504}}


class PagedArchive:
    """A deterministic S3 endpoint; everything above this boundary is real."""
    def __init__(self, keys, *, failures=(), before_request=None):
        self.keys = keys
        self.failures = set(failures)
        self.before_request = before_request
        self.calls = []
        self.lock = threading.Lock()

    def list_objects_v2(self, **request):
        prefix, cursor = request["Prefix"], request.get("ContinuationToken")
        with self.lock:
            self.calls.append(dict(request))
        if self.before_request:
            self.before_request(request)
        if (prefix, cursor) in self.failures:
            raise GatewayTimeout("injected late listing page 504")
        rows, directories = [], set()
        for key, size in sorted(self.keys.items()):
            if not key.startswith(prefix):
                continue
            relative = key[len(prefix):]
            if request.get("Delimiter") and "/" in relative:
                directories.add(prefix + relative.split("/", 1)[0] + "/")
            else:
                rows.append((key, "object", {"Key": key, "Size": size, "LastModified": dt.datetime.now(dt.timezone.utc)}))
        rows.extend((key, "prefix", {"Prefix": key}) for key in directories)
        rows.sort()
        start = int(cursor or 0)
        stop = start + request["MaxKeys"]
        page = rows[start:stop]
        result = {"Name": request["Bucket"], "Prefix": prefix, "KeyCount": len(page),
                  "IsTruncated": stop < len(rows),
                  "Contents": [value for _, kind, value in page if kind == "object"],
                  "CommonPrefixes": [value for _, kind, value in page if kind == "prefix"]}
        if result["IsTruncated"]:
            result["NextContinuationToken"] = str(stop)
        return result


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.keys = {}
        jobs = []
        for name in FAMILIES:
            source = self.root / "source" / name
            (source / "cl61").mkdir(parents=True)
            for number in range(7):
                path = source / "cl61" / f"{number}.dat"
                path.write_bytes(b"archive")
                settled = time.time() - 6 * 3600
                os.utime(path, (settled, settled))
                self.keys[f"{name}/cl61/{number}.dat"] = path.stat().st_size
            jobs.append({"name": name, "source": str(source), "destination": name,
                         "gws_destination": f"/test-gws/{name}", "shard_all_prefixes": True,
                         "verification_settle_age": "15m"})
        self.config = {"jobs": jobs, "streams": [{"name": "cl61"}], "bucket": "test-archive", "remote": "test",
                       "manifest_root": str(self.root / "manifests"),
                       "recovery_root": str(self.root / "recovery"),
                       "gws_manifest_root": str(self.root / "gws"),
                       "gate_state_path": str(self.root / "gate.json"),
                       "recovery_enabled": True, "recovery_s3_page_size": 2,
                       "recovery_free_reserve_bytes": 0, "recovery_cache_max_bytes": 10 * 1024 * 1024,
                       "domain_evidence_max_age_hours": {"raw_retention": 8, "products": 36}}
        summary = self.root / "gws/latest/summary.json"
        summary.parent.mkdir(parents=True)
        summary.write_text(json.dumps({"generated_at": recovery.iso(), "streams": {"cl61": {
            field: 0 for field in ("retention_local_missing_count", "retention_local_mismatch_count",
                                   "retention_gws_missing_count", "retention_gws_mismatch_count")}}}))
        self.inventory = recovery.load_inventory()
        def gws_inventory(config, job):
            return self.inventory.local_inventory(job["source"], self.inventory.COMMON_EXCLUDES, "15m")
        self.inventory.gws_inventory = mock.Mock(side_effect=gws_inventory)
        self.inventory.mirror_manifest_inventory = mock.Mock(side_effect=lambda config, job, kind: gws_inventory(config, job))
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(recovery, "load_inventory", return_value=self.inventory).start()
        self.client = PagedArchive(self.keys)
        mock.patch.object(reader, "_client", side_effect=lambda config: self.client).start()

    def queue(self):
        return recovery.Queue(self.config)

    def run_family(self, name):
        # The real worker intentionally returns zero when retry is durable.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(recovery.run_worker(self.config, name), 0)

    def make_due(self, name):
        # Advance only scheduler eligibility, never observation timestamps or
        # certificate IDs. Each invocation still collects a new real snapshot.
        queue = self.queue()
        try:
            with queue.db:
                queue.db.execute("UPDATE jobs SET next_retry_at=0 WHERE name=?", (name,))
        finally:
            queue.close()

    def snapshot(self):
        with evidence.read_snapshot(self.config) as snapshot:
            return snapshot.report, snapshot.gate

    def test_four_successful_families_survive_raw_and_products_late_page_failures(self):
        queue = self.queue()
        queue.enqueue(daily=True, confirmations=2, batch_id="acceptance-day")
        queue.close()
        self.client.failures = {(f"{name}/cl61/", "4") for name in ("raw", "products")}
        for name in FAMILIES[2:]:
            self.run_family(name)
        for name in ("raw", "products"):
            self.run_family(name)
        report, gate = self.snapshot()
        self.assertEqual(set(report["jobs"]), set(FAMILIES[2:]))
        self.assertFalse(gate["stable_parity"])
        for name in ("raw", "products"):
            self.assertEqual(gate["families"][name]["clean_streak"], 0)
        queue = self.queue()
        failed_epochs = {name: queue.row(name)["verification_id"] for name in ("raw", "products")}
        self.assertIsNone(queue.status()["daily_audits"]["acceptance-day"]["completed_at"])
        for name in failed_epochs:
            row = queue.row(name)
            self.assertEqual(row["state"], "retry_wait")
            self.assertGreater(row["next_retry_at"], time.time())
            self.assertEqual(json.loads(row["progress"])["pages_completed"], 2)
        queue.close()

        # Recreate both coordinator connections and S3 clients, as after a
        # process restart. Committed pages must not be fetched again.
        first_ids = {}
        for name in ("raw", "products"):
            self.make_due(name)
            self.client = PagedArchive(self.keys)
            self.run_family(name)
            report, gate = self.snapshot()
            first_ids[name] = report["jobs"][name]["verification_id"]
            self.assertEqual(first_ids[name], failed_epochs[name])
            requests = [call for call in self.client.calls if call["Prefix"] == f"{name}/cl61/"]
            self.assertEqual([call.get("ContinuationToken") for call in requests], ["4", "6"])
            self.assertEqual(gate["families"][name]["clean_streak"], 1)
            self.assertFalse((self.root / "recovery/epochs" / failed_epochs[name]).exists())
            queue = self.queue()
            self.assertGreaterEqual(queue.row(name)["next_retry_at"] - time.time(), 599)
            queue.close()
            calls_before_cooldown = len(self.client.calls)
            self.run_family(name)
            self.assertEqual(len(self.client.calls), calls_before_cooldown)
            self.assertEqual(self.snapshot()[1]["families"][name]["clean_streak"], 1)
        queue = self.queue()
        self.assertIsNotNone(queue.status()["daily_audits"]["acceptance-day"]["completed_at"])
        queue.close()
        self.assertFalse(gate["products_stable_parity"])
        for name in FAMILIES:
            self.make_due(name)
            self.client = PagedArchive(self.keys)
            self.run_family(name)
        report, gate = self.snapshot()
        self.assertTrue(gate["raw_retention_ready"], gate["failures"])
        self.assertTrue(gate["products_stable_parity"], gate["failures"])
        self.assertTrue(gate["stable_parity"])
        for name in FAMILIES:
            family = gate["families"][name]
            self.assertEqual(family["clean_streak"], 2)
            self.assertEqual(len({row["verification_id"] for row in family["observations"]}), 2)
        for name, identifier in first_ids.items():
            self.assertNotEqual(report["jobs"][name]["verification_id"], identifier)

    def test_products_collection_releases_shared_commit_lock_and_reserves_raw_lane(self):
        entered, release = threading.Event(), threading.Event()
        def wait_for_products(request):
            if request["Prefix"].startswith("products/"):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test did not release products worker")
        self.client.before_request = wait_for_products
        queue = self.queue()
        queue.enqueue(["products"])
        queue.close()
        worker = threading.Thread(target=self.run_family, args=("products",))
        worker.start()
        def stop_worker():
            release.set()
            worker.join(5)
        self.addCleanup(stop_worker)
        self.assertTrue(entered.wait(3))
        try:
            queue = self.queue()
            queue.enqueue(["raw", "manifests"])
            launched = []
            queue.launch_ready(launched.append)
            self.assertEqual(launched, ["raw"])
            queue.close()
            self.run_family("raw")
            report, gate = self.snapshot()
            self.assertIn("raw", report["jobs"])
            self.assertNotIn("products", report["jobs"])
            self.assertEqual(gate["families"]["raw"]["clean_streak"], 1)
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        report, _ = self.snapshot()
        self.assertEqual(set(report["jobs"]), {"raw", "products"})

    def test_repair_invalidation_at_publication_rejects_finished_old_observation(self):
        queue = self.queue()
        queue.enqueue(["products"], confirmations=2)
        queue.close()
        self.run_family("products")
        previous_report, _ = self.snapshot()
        previous_pointer = (self.root / "manifests/latest").resolve()
        publish = evidence.publish_family
        def invalidate_then_publish(config, name, values, artifacts, **kwargs):
            queue = self.queue()
            try:
                with recovery.commit_lock(config):
                    self.assertTrue(queue.invalidate(name, "repair-during-publication"))
            finally:
                queue.close()
            return publish(config, name, values, artifacts, **kwargs)
        self.make_due("products")
        with mock.patch.object(evidence, "publish_family", side_effect=invalidate_then_publish):
            self.run_family("products")
        report, _ = self.snapshot()
        self.assertEqual(report, previous_report)
        self.assertEqual((self.root / "manifests/latest").resolve(), previous_pointer)
        queue = self.queue()
        self.assertEqual(queue.row("products")["state"], "repairing")
        queue.repair_finished("products", "repair-during-publication")
        queue.close()
        after_repair_ids = []
        for _ in range(2):
            self.make_due("products")
            self.run_family("products")
            report, gate = self.snapshot()
            after_repair_ids.append(report["jobs"]["products"]["verification_id"])
        self.assertEqual(len(set(after_repair_ids)), 2)
        self.assertEqual({row["verification_id"] for row in gate["families"]["products"]["observations"]}, set(after_repair_ids))
        queue = self.queue()
        self.assertEqual(queue.row("products")["state"], "idle")
        queue.close()

    def test_coordinator_no_launch_persists_intent_and_heartbeat_without_systemctl(self):
        catalog = self.root / "catalog.json"
        catalog.write_text(json.dumps(self.config))
        arguments = ["aurora-object-store-recovery", "--catalog", str(catalog), "tick", "--no-launch"]
        with mock.patch.object(recovery, "systemd_active", return_value=False), mock.patch.object(recovery.subprocess, "run") as systemctl, mock.patch.object(sys, "argv", arguments):
            self.assertEqual(recovery.main(), 0)
        systemctl.assert_not_called()
        status = json.loads((self.root / "recovery/status.json").read_text())
        self.assertEqual(status["state"], "queued")
        self.assertTrue(status["heartbeat_at"])
        self.assertEqual(set(status["jobs"]), set(FAMILIES))
        self.assertFalse(status["last_full_audit_at"])
        self.assertFalse((self.root / "manifests/latest/comparison.json").exists())
        queue = self.queue()
        self.assertTrue(all(row["state"] == "queued" for row in queue.rows()))
        self.assertTrue(all(row["verification_id"] is None for row in queue.rows()))
        queue.close()


if __name__ == "__main__":
    unittest.main()
