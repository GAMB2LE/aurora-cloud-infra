import datetime as dt
import copy
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


def load_settled_source_status():
    source = TEMPLATE.read_text(encoding="utf-8")
    function_source = "def settled_source_status" + source.split(
        "def settled_source_status", 1
    )[1].split("\n\ndef operator_status", 1)[0]
    namespace = {}
    exec(function_source, namespace)
    return namespace["settled_source_status"]


settled_source_status = load_settled_source_status()


def collect_main_gws_evidence(summary):
    """Execute main's real ingestion path without live services or publication."""
    source = TEMPLATE.read_text(encoding="utf-8")
    age_source = "def evidence_age_hours" + source.split("def evidence_age_hours", 1)[1].split(
        "\n\ndef recovery_status", 1
    )[0]
    main_source = "def collect():" + source.split("def main() -> int:", 1)[1].split(
        "\n    object_missing = 0", 1
    )[0] + "\n    return metrics, failures, retention_streams\n"
    namespace = {
        "dt": dt, "GWS": "gws", "INVENTORY_PROGRESS": "progress", "RECOVERY_STATUS": "recovery",
        "DISPATCH": "dispatch", "MENAPIA_FLIGHT_STATUS": "menapia", "DISPATCH_DATABASE": "db",
        "RECOVERY_ENABLED": False, "RAW_EVIDENCE_MAX_AGE_HOURS": 8, "OBJECT_REPORT_MAX_AGE_HOURS": 36,
        "STREAMS": [{"name": "hatprog5", "prune_enabled": True}], "PREFIX": {"hatprog5": "hatpro"},
        "read": lambda path: copy.deepcopy(summary) if path == "gws" else {},
        "archive_snapshot": lambda: ({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}, {}),
        "recovery_status": lambda *args, **kwargs: {"failures": []},
        "prefix_delivery": lambda *args, **kwargs: {},
        "settled_source_status": settled_source_status,
    }
    exec(age_source + "\n\n" + main_source, namespace)
    return namespace["collect"]()


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

    def settled_fixture(self, current, **counts):
        return {
            "generated_at": current.isoformat(),
            "gws_available": True,
            "streams": {"hatprog5": {
                "source_count": 38, "local_count": 38, "gws_count": 38,
                "local_missing_count": 0, "local_mismatch_count": 0,
                "gws_missing_count": 0, "gws_mismatch_count": 0,
                "retention_local_missing_count": 0, "retention_local_mismatch_count": 0,
                "retention_gws_missing_count": 0, "retention_gws_mismatch_count": 0,
                "prune_ready": True,
                **counts,
            }},
        }

    def settled_presentation(self, summary, current, gate=None, progress=None):
        settled = settled_source_status(summary, [{"name": "hatprog5", "prune_enabled": True}], current)
        metrics = {**self.base_metrics(), **settled["metrics"]}
        result = operator_status(
            settled["failures"], metrics, gate or self.clean_gate(current), progress or {"state": "idle"}
        )
        return settled, result

    def test_settled_path_gaps_alert_without_changing_retention_eligibility(self):
        current = dt.datetime.now(dt.timezone.utc)
        for field, wording in (
            ("local_missing_count", "38 missing paths at configured cloud"),
            ("gws_missing_count", "38 missing paths at configured GWS"),
            ("local_mismatch_count", "38 size mismatches at configured cloud"),
            ("gws_mismatch_count", "38 size mismatches at configured GWS"),
        ):
            with self.subTest(field=field):
                summary = self.settled_fixture(current, **{field: 38})
                gate = self.clean_gate(current)
                before = copy.deepcopy((summary, gate))
                settled, result = self.settled_presentation(summary, current, gate, {"state": "running"})
                self.assertEqual(settled["state"], "current")
                self.assertEqual(settled["affected_streams"], ["hatprog5"])
                self.assertEqual(settled["metrics"][f"settled_source_{field}"], 38)
                self.assertEqual(settled["metrics"][f"settled_source_hatprog5_{field}"], 38)
                self.assertEqual(result["level"], "red")
                self.assertEqual(result["title"], "Archive paths are incomplete")
                self.assertIn(wording, result["detail"])
                self.assertIn("hatprog5", result["detail"])
                self.assertIn("Raw retention evidence remains independently clean", result["detail"])
                self.assertFalse(result["pruning_paused"])
                self.assertNotIn("recheck is running", result["detail"])
                self.assertNotIn("object storage", result["detail"])
                self.assertEqual((summary, gate), before)

    def test_cloud_and_gws_counts_are_separate_not_a_claim_of_unique_missing_files(self):
        current = dt.datetime.now(dt.timezone.utc)
        _, result = self.settled_presentation(
            self.settled_fixture(current, local_missing_count=38, gws_missing_count=38), current
        )
        self.assertIn("38 missing paths at configured cloud", result["detail"])
        self.assertIn("38 missing paths at configured GWS", result["detail"])
        self.assertNotIn("76", result["detail"])

    def test_settled_path_alert_preserves_an_independently_paused_raw_gate(self):
        current = dt.datetime.now(dt.timezone.utc)
        gate = self.clean_gate(current)
        gate["raw_retention_ready"] = False
        _, result = self.settled_presentation(self.settled_fixture(current, gws_missing_count=1), current, gate)
        self.assertEqual(result["level"], "red")
        self.assertTrue(result["pruning_paused"])

    def test_clean_settled_paths_with_transient_recovery_remain_green(self):
        current = dt.datetime.now(dt.timezone.utc)
        settled, _ = self.settled_presentation(self.settled_fixture(current), current)
        recovery = recovery_status(self.recovery_fixture(current), {}, True, current)
        result = operator_status([], {**self.base_metrics(), **settled["metrics"]}, self.clean_gate(current),
                                 recovery_progress(recovery), recovery=recovery)
        self.assertEqual(settled["affected_streams"], [])
        self.assertEqual(result["level"], "green")
        self.assertIn("retrying automatically", result["detail"])
        self.assertFalse(result["pruning_paused"])

    def test_stale_or_unavailable_settled_summary_cannot_assert_current_gaps(self):
        current = dt.datetime.now(dt.timezone.utc)
        examples = []
        for age in (8, 9):
            summary = self.settled_fixture(current - dt.timedelta(hours=age), gws_missing_count=38)
            examples.append(summary)
        examples.extend([
            {**self.settled_fixture(current, gws_missing_count=38), "gws_available": False},
            {**self.settled_fixture(current), "generated_at": "invalid"},
            {**self.settled_fixture(current), "generated_at": current.replace(tzinfo=None).isoformat()},
            {**self.settled_fixture(current), "generated_at": "9999-12-31T23:59:00+00:00"},
            self.settled_fixture(current + dt.timedelta(minutes=6)),
            {}, None,
        ])
        for summary in examples:
            with self.subTest(summary=summary):
                settled, result = self.settled_presentation(summary, current)
                self.assertEqual(settled["state"], "unavailable")
                self.assertIsNone(settled["metrics"]["settled_source_gws_missing_count"])
                self.assertEqual(settled["affected_streams"], [])
                self.assertEqual(result["level"], "amber")
                self.assertEqual(result["title"], "Archive verification is overdue")
                self.assertNotIn("38", result["detail"])
                # Presentation alone cannot revoke a separate current gate.
                self.assertFalse(result["pruning_paused"])

    def test_malformed_or_missing_stream_counts_are_unknown_not_clean(self):
        current = dt.datetime.now(dt.timezone.utc)
        examples = []
        for value in (None, "38", -1, True, 1.5):
            examples.append(self.settled_fixture(current, local_missing_count=value))
        for state in ({}, [], {"error": "remote collection failed"}):
            examples.append({**self.settled_fixture(current), "streams": {"hatprog5": state}})
        examples.append({**self.settled_fixture(current), "streams": []})
        for summary in examples:
            with self.subTest(summary=summary):
                settled, result = self.settled_presentation(summary, current)
                self.assertIsNone(settled["metrics"]["settled_source_local_missing_count"])
                self.assertEqual(result["level"], "amber")
                self.assertFalse(result["pruning_paused"])

    def test_fresh_confirmed_gaps_remain_visible_when_another_stream_is_unknown(self):
        current = dt.datetime.now(dt.timezone.utc)
        summary = self.settled_fixture(current, gws_missing_count=38)
        settled = settled_source_status(summary, [{"name": "hatprog5"}, {"name": "cl61"}], current)
        result = operator_status(settled["failures"], {**self.base_metrics(), **settled["metrics"]},
                                 self.clean_gate(current), {})
        self.assertEqual(settled["state"], "unavailable")
        self.assertEqual(settled["metrics"]["settled_source_gws_missing_count"], 38)
        self.assertEqual(result["level"], "red")
        self.assertFalse(result["pruning_paused"])

    def test_planned_offline_stream_does_not_manufacture_missing_evidence(self):
        current = dt.datetime.now(dt.timezone.utc)
        summary = self.settled_fixture(current)
        summary["streams"]["offline"] = {"planned_offline": True}
        settled = settled_source_status(summary, [{"name": "hatprog5"}, {"name": "offline"}], current)
        self.assertEqual(settled["state"], "current")
        self.assertEqual(settled["metrics"]["settled_source_gws_missing_count"], 0)
        self.assertEqual(settled["streams"]["offline"]["state"], "planned_offline")

    def test_health_main_publishes_separate_settled_path_evidence(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("settled_paths = settled_source_status(gws, STREAMS", source)
        self.assertIn('metrics.update(settled_paths["metrics"])', source)
        self.assertIn('"settled_source_paths":', source)
        self.assertIn("issues += retention_missing + retention_mismatch", source)

    def test_main_ingestion_handles_malformed_stream_container_and_timestamp(self):
        current = dt.datetime.now(dt.timezone.utc)
        examples = [{**self.settled_fixture(current), "streams": value} for value in (None, [])]
        examples.extend({**self.settled_fixture(current), "generated_at": value} for value in (
            "invalid", current.replace(tzinfo=None).isoformat(), "9999-12-31T23:59:00+00:00"
        ))
        for summary in examples:
            with self.subTest(summary=summary):
                metrics, failures, _ = collect_main_gws_evidence(summary)
                self.assertEqual(metrics["settled_source_evidence_available_state"], 0)
                self.assertIsNone(metrics["settled_source_gws_missing_count"])
                self.assertIsInstance(metrics["hatpro_gws_missing_count"], int)
                result = operator_status(failures, {**self.base_metrics(), **metrics}, self.clean_gate(current), {})
                self.assertEqual(result["level"], "amber")
                self.assertEqual(result["title"], "Archive verification is overdue")

    def test_main_preserves_integer_legacy_telemetry_without_certifying_stale_gaps(self):
        current = dt.datetime.now(dt.timezone.utc)
        for age, count in ((0, 38), (9, 38), (0, "38"), (0, "invalid")):
            with self.subTest(age=age, count=count):
                summary = self.settled_fixture(current - dt.timedelta(hours=age), gws_missing_count=count)
                metrics, failures, retention = collect_main_gws_evidence(summary)
                self.assertIsInstance(metrics["hatpro_gws_missing_count"], int)
                self.assertEqual(metrics["hatpro_gws_missing_count"], 0 if count == "invalid" else 38)
                self.assertEqual(retention, [{"id": "hatprog5", "enabled": True, "ready": True,
                                             "gws_missing": 0, "gws_mismatch": 0}])
                result = operator_status(failures, {**self.base_metrics(), **metrics}, self.clean_gate(current), {})
                if age == 0 and isinstance(count, int):
                    self.assertEqual(result["level"], "red")
                    self.assertFalse(result["pruning_paused"])
                else:
                    self.assertIsNone(metrics["settled_source_gws_missing_count"])
                    self.assertEqual(result["level"], "amber")

    def confirmation_fixture(self, name, starts):
        observations = [
            {"verification_id": f"{name}-{index}", "evidence_started_at": started.isoformat(),
             "verification_completed_at": (started + dt.timedelta(minutes=2)).isoformat(), "start_trusted": True}
            for index, started in enumerate(starts)
        ]
        latest = observations[-1]
        return {"jobs": {name: latest}}, {"families": {name: {
            **latest, "evidence_start_trusted": True, "observations": observations,
            "confirmation_evidence_started_at": min(starts).isoformat(),
            "clean_streak": len(observations), "stable_parity": len(observations) >= 2,
        }}}

    def test_recovery_raw_age_and_expiry_use_oldest_required_confirmation(self):
        current = dt.datetime(2026, 9, 6, 12, 40, tzinfo=dt.timezone.utc)
        first = current.replace(hour=11, minute=30, second=38)
        second = current.replace(hour=12, minute=0, second=40)
        report, gate = self.confirmation_fixture("raw", [first, second])
        state = recovery_status(self.recovery_fixture(current), report, True, current, gate=gate)
        raw = state["jobs"]["raw"]
        self.assertEqual(raw["observation_age_hours"], (current - first).total_seconds() / 3600)
        self.assertEqual(raw["evidence_expires_at"], "2026-09-06T19:30:38+00:00")

    def test_recovery_product_families_keep_independent_confirmation_clocks(self):
        current = dt.datetime(2026, 9, 6, 12, 40, tzinfo=dt.timezone.utc)
        report, gate = {"jobs": {}}, {"families": {}}
        for name, age in (("products", 20), ("products-wxcam", 10), ("manifests", 2)):
            family_report, family_gate = self.confirmation_fixture(
                name, [current - dt.timedelta(hours=age), current - dt.timedelta(hours=age - 1)])
            report["jobs"].update(family_report["jobs"])
            gate["families"].update(family_gate["families"])
        before = recovery_status(self.recovery_fixture(current), report, True, current, gate=gate)
        updated_report, updated_gate = self.confirmation_fixture(
            "manifests", [current - dt.timedelta(hours=1), current])
        report["jobs"].update(updated_report["jobs"])
        gate["families"].update(updated_gate["families"])
        after = recovery_status(self.recovery_fixture(current), report, True, current, gate=gate)
        for name, age in (("products", 20), ("products-wxcam", 10)):
            with self.subTest(name=name):
                self.assertEqual(after["jobs"][name]["observation_age_hours"], age)
                self.assertEqual(after["jobs"][name]["evidence_expires_at"],
                                 (current + dt.timedelta(hours=36 - age)).isoformat())
                self.assertEqual(after["jobs"][name], before["jobs"][name])
        self.assertEqual(after["jobs"]["manifests"]["observation_age_hours"], 1)

    def test_recovery_confirmation_expiry_does_not_move_without_new_evidence(self):
        current = dt.datetime(2026, 9, 6, 12, 40, tzinfo=dt.timezone.utc)
        first, second = current - dt.timedelta(hours=7), current - dt.timedelta(hours=1)
        report, gate = self.confirmation_fixture("raw", [first, second])
        before = recovery_status(self.recovery_fixture(current), report, True, current, gate=gate)
        later = current + dt.timedelta(hours=2)
        after = recovery_status(self.recovery_fixture(later), report, True, later, gate=gate)
        self.assertEqual(before["jobs"]["raw"]["observation_age_hours"], 7)
        self.assertEqual(after["jobs"]["raw"]["observation_age_hours"], 9)
        self.assertEqual(after["jobs"]["raw"]["evidence_expires_at"], before["jobs"]["raw"]["evidence_expires_at"])
        self.assertLess(dt.datetime.fromisoformat(after["jobs"]["raw"]["evidence_expires_at"]), later)

    def test_recovery_first_confirmation_uses_its_trusted_observation_start(self):
        current = dt.datetime(2026, 9, 6, 12, 40, tzinfo=dt.timezone.utc)
        started = current - dt.timedelta(hours=1)
        report, gate = self.confirmation_fixture("raw", [started])
        for supplied_gate in (gate, {}):
            with self.subTest(gate_present=bool(supplied_gate)):
                state = recovery_status(self.recovery_fixture(current), report, True, current, gate=supplied_gate)
                self.assertEqual(state["jobs"]["raw"]["observation_age_hours"], 1)
                self.assertEqual(state["jobs"]["raw"]["evidence_expires_at"],
                                 (started + dt.timedelta(hours=8)).isoformat())

    def test_in_flight_queue_diagnostics_do_not_reset_published_evidence_age(self):
        current = dt.datetime(2026, 9, 6, 12, 40, tzinfo=dt.timezone.utc)
        report, gate = self.confirmation_fixture(
            "products", [current - dt.timedelta(hours=20), current - dt.timedelta(hours=10)])
        queue = self.recovery_fixture(current, state="running", verification_id="products-in-flight",
                                      evidence_started_at=current.isoformat(),
                                      expires_at=(current + dt.timedelta(hours=12)).isoformat())
        job = recovery_status(queue, report, True, current, gate=gate)["jobs"]["products"]
        self.assertEqual(job["verification_id"], "products-in-flight")
        self.assertEqual(job["evidence_started_at"], current.isoformat())
        self.assertEqual(job["expires_at"], (current + dt.timedelta(hours=12)).isoformat())
        self.assertEqual(job["progress"], {"pages": 400})
        self.assertEqual(job["observation_age_hours"], 20)
        self.assertEqual(job["evidence_expires_at"], (current + dt.timedelta(hours=16)).isoformat())

    def test_recovery_does_not_mix_an_unmatched_gate_with_report_evidence(self):
        current = dt.datetime(2026, 9, 6, 12, 40, tzinfo=dt.timezone.utc)
        report, gate = self.confirmation_fixture(
            "raw", [current - dt.timedelta(hours=2), current - dt.timedelta(hours=1)])
        gate["families"]["raw"]["verification_id"] = "unmatched-observation"
        state = recovery_status(self.recovery_fixture(current), report, True, current, gate=gate)
        self.assertEqual(state["jobs"]["raw"]["observation_age_hours"], 1)
        self.assertEqual(state["jobs"]["raw"]["evidence_expires_at"],
                         (current + dt.timedelta(hours=7)).isoformat())

    def test_recovery_gate_metadata_cannot_extend_latest_trusted_evidence(self):
        current = dt.datetime(2026, 9, 6, 12, 40, tzinfo=dt.timezone.utc)
        report, gate = self.confirmation_fixture("raw", [current - dt.timedelta(hours=1)])
        for floor in (None, "not-a-timestamp", current.isoformat(), (current + dt.timedelta(hours=10)).isoformat()):
            with self.subTest(floor=floor):
                gate["families"]["raw"]["confirmation_evidence_started_at"] = floor
                state = recovery_status(self.recovery_fixture(current), report, True, current, gate=gate)
                self.assertEqual(state["jobs"]["raw"]["observation_age_hours"], 1)
                self.assertEqual(state["jobs"]["raw"]["evidence_expires_at"],
                                 (current + dt.timedelta(hours=7)).isoformat())

    def migration_gate(self, current):
        gate = self.clean_gate(current)
        gate.update(clean=False, stable_parity=False, raw_retention_ready=False)
        for name, domain in gate["domains"].items():
            family = "raw" if name == "raw_retention" else "products"
            domain.update(clean=False, stable_parity=False, clean_streak=0,
                          failures=[f"{family}:observation_start_untrusted", f"{family}:verification_timestamp_invalid"])
        return gate

    def test_untrusted_legacy_starts_are_overdue_while_automatic_observations_run(self):
        current = dt.datetime.now(dt.timezone.utc)
        recovery = {"enabled": True, "state": "running", "active_alert": False,
                    "detail": "Automatic archive verification is running for products, raw."}
        result = operator_status(["object_store_stable_parity=false"], self.base_metrics(),
                                 self.migration_gate(current), {"state": "running"}, recovery=recovery)
        self.assertEqual(result["level"], "amber")
        self.assertEqual(result["title"], "Archive verification is overdue")
        self.assertIn("trusted evidence", result["detail"])
        self.assertIn("running for products, raw", result["detail"])
        self.assertTrue(result["pruning_paused"])
        self.assertNotIn("service health", result["detail"])

    def test_bootstrap_missing_family_proof_is_amber_without_weakening_raw_gate(self):
        current = dt.datetime.now(dt.timezone.utc)
        gate = self.clean_gate(current)
        gate["domains"]["products"].update(clean=False, stable_parity=False,
                                          failures=["products:configured_family_missing"])
        result = operator_status(["object_store_stable_parity=false"], self.base_metrics(), gate, {},
                                 recovery={"enabled": True, "state": "queued", "active_alert": False})
        self.assertEqual(result["level"], "amber")
        self.assertFalse(result["pruning_paused"])
        self.assertIn("Raw retention evidence remains independently current", result["detail"])

    def test_migration_presentation_does_not_hide_service_failures_or_measured_gaps(self):
        current = dt.datetime.now(dt.timezone.utc)
        recovery = {"enabled": True, "state": "running", "active_alert": False}
        for service in ("aurora-object-store-inventory.service", "aurora-object-store-copy-raw.service"):
            with self.subTest(service=service):
                result = operator_status([f"archive_service_unhealthy={service}", "object_store_stable_parity=false"],
                                         self.base_metrics(), self.migration_gate(current), {}, recovery=recovery)
                self.assertEqual(result["level"], "red")
        metrics = self.base_metrics()
        metrics["object_store_all_missing_count"] = 2
        result = operator_status(["object_store_stable_parity=false"], metrics, self.migration_gate(current), {}, recovery=recovery)
        self.assertEqual(result["level"], "red")
        self.assertEqual(result["title"], "Archive copies are incomplete")

    def test_legacy_completion_time_cannot_be_shown_as_trusted_observation_expiry(self):
        current = dt.datetime.now(dt.timezone.utc)
        legacy = {"jobs": {"products": {"verified_at": current.isoformat()}}}
        _, gate = self.confirmation_fixture("products", [current - dt.timedelta(hours=1)])
        state = recovery_status(self.recovery_fixture(current), legacy, True, current, gate=gate)
        self.assertIsNone(state["jobs"]["products"]["observation_age_hours"])
        self.assertIsNone(state["jobs"]["products"]["evidence_expires_at"])

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
