import datetime as dt
from pathlib import Path
import sqlite3
import tempfile
import unittest


TEMPLATE = (
    Path(__file__).parents[1]
    / "roles/operations_monitor/templates/aurora-archive-health.py.j2"
)


def load_operator_status():
    source = TEMPLATE.read_text(encoding="utf-8")
    function_source = "def operator_status" + source.split(
        "def operator_status", 1
    )[1].split("\n\ndef main", 1)[0]
    namespace = {}
    exec(function_source, namespace)
    return namespace["operator_status"]


operator_status = load_operator_status()


def load_prefix_delivery():
    source = TEMPLATE.read_text(encoding="utf-8")
    function_source = "def prefix_delivery" + source.split(
        "def prefix_delivery", 1
    )[1].split("\n\ndef source_sync_pairs", 1)[0]
    namespace = {"Path": Path, "sqlite3": sqlite3}
    exec(function_source, namespace)
    return namespace["prefix_delivery"]


prefix_delivery = load_prefix_delivery()


class ArchiveHealthPresentationTests(unittest.TestCase):
    def base_metrics(self):
        return {
            "streams_gws_issue_count": 0,
            "object_store_all_missing_count": 0,
            "object_store_all_mismatch_count": 0,
            "gws_all_missing_count": 0,
            "gws_all_mismatch_count": 0,
        }

    def test_missing_objects_are_reported_in_plain_language(self):
        metrics = self.base_metrics()
        metrics["object_store_all_missing_count"] = 439
        result = operator_status(
            ["object_store_all_missing=439", "object_store_stable_parity=false"],
            metrics,
            {"clean": False, "stable_parity": False},
            {"state": "running"},
        )

        self.assertEqual(result["level"], "red")
        self.assertEqual(result["title"], "Archive copies are incomplete")
        self.assertIn("439 settled files", result["detail"])
        self.assertIn("GWS copy is complete", result["detail"])
        self.assertIn("strict recheck is running", result["detail"])
        self.assertNotIn("object_store_", result["detail"])

    def test_first_clean_audit_stays_green_while_confirmation_is_pending(self):
        generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
        result = operator_status(
            ["object_store_stable_parity=false"],
            self.base_metrics(),
            {
                "clean": True,
                "stable_parity": False,
                "last_generated_at": generated_at,
            },
            {"state": "complete"},
        )

        self.assertEqual(result["level"], "green")
        self.assertEqual(result["title"], "Archive copies are healthy")
        self.assertIn("second retention confirmation", result["detail"])
        self.assertTrue(result["pruning_paused"])

    def test_incremental_repair_recheck_is_not_an_alert(self):
        generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
        result = operator_status(
            ["object_store_stable_parity=false"],
            self.base_metrics(),
            {
                "clean": True,
                "stable_parity": False,
                "verification_mode": "incremental",
                "last_generated_at": generated_at,
            },
            {"state": "complete"},
        )

        self.assertEqual(result["level"], "green")
        self.assertIn("last certified raw parity check is clean", result["detail"])
        self.assertTrue(result["pruning_paused"])

    def test_stale_clean_evidence_with_healthy_delivery_is_amber(self):
        last_clean_at = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=49)
        ).isoformat()
        result = operator_status(
            ["archive_service_unhealthy=aurora-object-store-inventory.service"],
            self.base_metrics(),
            {
                "clean": False,
                "stable_parity": False,
                "raw_retention_ready": False,
                "domains": {
                    "raw_retention": {
                        "clean": False,
                        "stable_parity": False,
                        "last_clean_at": last_clean_at,
                    }
                },
            },
            {
                "state": "failed",
                "completed_jobs": ["raw", "products"],
                "total_jobs": 5,
                "error": (
                    "all GWS inventory hosts failed: rrniii@"
                    "xfer-vm-03.jasmin.ac.uk: Permission denied (publickey)"
                ),
            },
        )

        self.assertEqual(result["level"], "amber")
        self.assertEqual(result["title"], "Archive verification is delayed")
        self.assertIn("rejected the verifier login", result["detail"])
        self.assertIn("2 of 5 archive families", result["detail"])
        self.assertIn("no settled archive gap", result["detail"])
        self.assertTrue(result["pruning_paused"])

    def test_menapia_delivery_is_scoped_to_its_raw_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "queue.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE delivery (
                        job TEXT,
                        relative_path TEXT,
                        gws_delivered INTEGER,
                        object_delivered INTEGER
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO delivery VALUES (?, ?, ?, ?)",
                    [
                        ("raw", "menapia/drone-uploads/a.bin", 1, 1),
                        ("raw", "menapia/drone-uploads/b.bin", 0, 1),
                        ("raw", "menapia/drone-uploads/c.bin", 1, 0),
                        ("raw", "cl61/unrelated.nc", 0, 0),
                        ("products", "menapia/not-raw.bin", 0, 0),
                    ],
                )
            result = prefix_delivery(database, "menapia/")

        self.assertTrue(result["available"])
        self.assertEqual(result["tracked_files"], 3)
        self.assertEqual(result["dual_delivered_files"], 1)
        self.assertEqual(result["gws_pending_files"], 1)
        self.assertEqual(result["object_store_pending_files"], 1)

    def test_health_contract_contains_menapia_source_and_archive_metrics(self):
        source = TEMPLATE.read_text(encoding="utf-8")

        self.assertIn('"source_ingest": {', source)
        self.assertIn('"menapia": {', source)
        self.assertIn('"menapia_flight_gws_pending_files"', source)
        self.assertIn('"menapia_flight_object_store_pending_files"', source)


if __name__ == "__main__":
    unittest.main()
