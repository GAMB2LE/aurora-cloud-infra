import datetime as dt
from pathlib import Path
import sqlite3
import tempfile
import unittest


TEMPLATE = (
    Path(__file__).parents[1]
    / "roles/operations_monitor/templates/aurora-archive-health.py.j2"
)
SERVICE_TEMPLATE = (
    Path(__file__).parents[1]
    / "roles/operations_monitor/templates/aurora-archive-health.service.j2"
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


def load_recovery_helpers():
    source = TEMPLATE.read_text(encoding="utf-8")
    function_source = "def recovery_status" + source.split("def recovery_status", 1)[1].split("\n\ndef operator_status", 1)[0]
    namespace = {"RAW_EVIDENCE_MAX_AGE_HOURS": 8, "OBJECT_REPORT_MAX_AGE_HOURS": 36}
    exec(function_source, namespace)
    return namespace["recovery_status"], namespace["recovery_progress"]


recovery_status, recovery_progress = load_recovery_helpers()


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
    def clean_gate(self, current):
        return {
            "clean": True,
            "stable_parity": True,
            "raw_retention_ready": True,
            "domains": {
                name: {"clean": True, "stable_parity": True, "evidence_floor_generated_at": current.isoformat()}
                for name in ("raw_retention", "products")
            },
        }

    def recovery_fixture(self, current, **job_overrides):
        return {
            "state": "retry_wait",
            "heartbeat_at": current.isoformat(),
            "next_retry_at": (current + dt.timedelta(minutes=15)).isoformat(),
            "jobs": {"products": {
                "state": "retry_wait",
                "first_failure_at": (current - dt.timedelta(minutes=30)).isoformat(),
                "error_class": "transient",
                "last_error": "JASMIN listing returned HTTP 504",
                "progress": {"pages": 400},
                **job_overrides,
            }},
        }

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

    def test_current_clean_evidence_and_retry_are_quiet_but_visible(self):
        current = dt.datetime.now(dt.timezone.utc)
        report = {"jobs": {"products": {"evidence_started_at": (current - dt.timedelta(hours=12)).isoformat()}}}
        recovery = recovery_status(self.recovery_fixture(current), report, True, current)
        result = operator_status([], self.base_metrics(), self.clean_gate(current), recovery_progress(recovery), recovery=recovery)
        self.assertEqual(result["level"], "green")
        self.assertIn("retrying automatically", result["detail"])
        self.assertFalse(recovery["active_alert"])
        self.assertEqual(recovery["jobs"]["products"]["progress"]["pages"], 400)
        self.assertEqual(recovery["jobs"]["products"]["observation_age_hours"], 12)
        self.assertEqual(recovery["affected_jobs"], ["products"])
        self.assertFalse(result["pruning_paused"])

    def test_blocked_or_six_hour_failure_escalates_even_with_clean_evidence(self):
        current = dt.datetime.now(dt.timezone.utc)
        for overrides, expected_level in (
            ({"state": "blocked", "error_class": "auth"}, "red"),
            ({"first_failure_at": (current - dt.timedelta(hours=6)).isoformat()}, "amber"),
        ):
            with self.subTest(overrides=overrides):
                recovery = recovery_status(self.recovery_fixture(current, **overrides), {}, True, current)
                result = operator_status(recovery["failures"], self.base_metrics(), self.clean_gate(current), {}, recovery=recovery)
                self.assertEqual(result["level"], expected_level)
                self.assertTrue(recovery["active_alert"])
                self.assertEqual(result["title"], "Archive verification recovery needs attention")

    def test_heartbeat_is_independent_of_worker_status_updates(self):
        current = dt.datetime.now(dt.timezone.utc)
        source = self.recovery_fixture(current)
        source["updated_at"] = current.isoformat()
        source["heartbeat_at"] = (current - dt.timedelta(minutes=16)).isoformat()
        recovery = recovery_status(source, {}, True, current)
        self.assertIn("archive_recovery_heartbeat_overdue", recovery["failures"])
        self.assertEqual(recovery["heartbeat_age_minutes"], 16)
        disabled = recovery_status({}, {}, False, current)
        self.assertEqual(disabled["failures"], [])

    def test_expiry_remains_an_alert_during_automatic_retry(self):
        current = dt.datetime.now(dt.timezone.utc)
        recovery = recovery_status(self.recovery_fixture(current), {}, True, current)
        gate = self.clean_gate(current)
        gate["domains"]["products"]["evidence_floor_generated_at"] = (current - dt.timedelta(hours=37)).isoformat()
        result = operator_status([], self.base_metrics(), gate, {}, recovery=recovery)
        self.assertEqual(result["level"], "amber")
        self.assertEqual(result["title"], "Product archive verification is overdue")
        self.assertNotIn("incomplete", result["title"])
        gate["domains"]["raw_retention"]["evidence_floor_generated_at"] = (current - dt.timedelta(hours=9)).isoformat()
        result = operator_status([], self.base_metrics(), gate, {}, recovery=recovery)
        self.assertEqual(result["title"], "Archive verification is overdue")
        self.assertTrue(result["pruning_paused"])

    def test_independent_gws_expiry_cannot_be_hidden_by_current_s3_evidence(self):
        current = dt.datetime.now(dt.timezone.utc)
        result = operator_status(["gws_evidence_stale_hours=9.01"], self.base_metrics(), self.clean_gate(current), {})
        self.assertEqual(result["title"], "Archive verification is overdue")
        self.assertEqual(result["level"], "amber")
        self.assertTrue(result["pruning_paused"])

    def test_daily_completion_requires_new_verification_ids(self):
        recovery = {"state": "queued", "jobs": {"raw": {"state": "idle"}, "products": {"state": "retry_wait"}},
                    "daily_audits": {"daily": {"requested_at": "2026-09-06T03:20:00Z", "jobs": {"raw": "new-id", "products": None}}}}
        progress = recovery_progress(recovery)
        self.assertEqual(progress["completed_jobs"], ["raw"])
        self.assertEqual(progress["state"], "queued")
        self.assertEqual(progress["total_jobs"], 2)

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

    def test_clean_products_can_be_fourteen_hours_old_while_raw_is_green(self):
        now = dt.datetime.now(dt.timezone.utc)
        result = operator_status(
            [],
            self.base_metrics(),
            {
                "clean": True,
                "stable_parity": True,
                "raw_retention_ready": True,
                "domains": {
                    "raw_retention": {
                        "clean": True,
                        "stable_parity": True,
                        "evidence_floor_generated_at": now.isoformat(),
                    },
                    "products": {
                        "clean": True,
                        "stable_parity": True,
                        "evidence_floor_generated_at": (
                            now - dt.timedelta(hours=14)
                        ).isoformat(),
                    },
                },
            },
            {"state": "complete"},
        )

        self.assertEqual(result["level"], "green")
        self.assertEqual(result["title"], "Archive copies are healthy")
        self.assertFalse(result["pruning_paused"])

    def test_expired_product_evidence_is_amber_without_pausing_raw(self):
        now = dt.datetime.now(dt.timezone.utc)
        result = operator_status(
            ["object_store_stable_parity=false"],
            self.base_metrics(),
            {
                "clean": False,
                "stable_parity": False,
                "raw_retention_ready": True,
                "domains": {
                    "raw_retention": {
                        "clean": True,
                        "stable_parity": True,
                        "evidence_floor_generated_at": now.isoformat(),
                    },
                    "products": {
                        "clean": False,
                        "stable_parity": False,
                        "evidence_floor_generated_at": (
                            now - dt.timedelta(hours=37)
                        ).isoformat(),
                    },
                },
            },
            {"state": "complete"},
        )

        self.assertEqual(result["level"], "amber")
        self.assertEqual(
            result["title"],
            "Product archive verification is overdue",
        )
        self.assertIn("beyond its 36-hour limit", result["detail"])
        self.assertFalse(result["pruning_paused"])

    def test_raw_evidence_floor_age_overrides_cached_ready_state(self):
        now = dt.datetime.now(dt.timezone.utc)
        result = operator_status(
            [],
            self.base_metrics(),
            {
                "clean": True,
                "stable_parity": True,
                "raw_retention_ready": True,
                "domains": {
                    "raw_retention": {
                        "clean": True,
                        "stable_parity": True,
                        "last_clean_at": now.isoformat(),
                        "evidence_floor_generated_at": (
                            now - dt.timedelta(hours=9)
                        ).isoformat(),
                    }
                },
            },
            {"state": "complete"},
        )

        self.assertEqual(result["level"], "amber")
        self.assertTrue(result["pruning_paused"])
        self.assertIn("certified raw evidence", result["detail"].lower())

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
                        ("raw", "menapia/menapia_mqtt.log", 0, 0),
                        ("raw", "cl61/unrelated.nc", 0, 0),
                        ("products", "menapia/not-raw.bin", 0, 0),
                    ],
                )
            result = prefix_delivery(database, "menapia/drone-uploads/")

        self.assertTrue(result["available"])
        self.assertEqual(result["tracked_files"], 3)
        self.assertEqual(result["dual_delivered_files"], 1)
        self.assertEqual(result["gws_pending_files"], 1)
        self.assertEqual(result["object_store_pending_files"], 1)

    def test_health_service_can_open_dispatcher_sqlite_state(self):
        source = SERVICE_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("{{ archive_monitor_output_root }}", source)
        self.assertIn("{{ archive_dispatch_state_root }}", source)

    def test_health_contract_contains_menapia_source_and_archive_metrics(self):
        source = TEMPLATE.read_text(encoding="utf-8")

        self.assertIn('"source_ingest": {', source)
        self.assertIn('"menapia": {', source)
        self.assertIn('"menapia_flight_gws_pending_files"', source)
        self.assertIn('"menapia_flight_object_store_pending_files"', source)
        self.assertIn('"menapia/drone-uploads/"', source)


if __name__ == "__main__":
    unittest.main()
