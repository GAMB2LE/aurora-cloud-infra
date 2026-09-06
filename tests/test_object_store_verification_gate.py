from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "roles/object_store_mirror/files/aurora-object-store-verification-gate.py"
SPEC = importlib.util.spec_from_file_location("object_store_verification_gate", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
PRODUCT_JOBS = ("products", "products-wxcam", "model-evaluation", "menapia-flight-manifests", "manifests")
ALL_JOBS = ("raw", *PRODUCT_JOBS)
NOW = dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc)


def config(names=ALL_JOBS):
    return {"jobs": [{"name": name} for name in names], "streams": [],
            "domain_evidence_max_age_hours": {"raw_retention": 8, "products": 36},
            "_gws_summary": {"generated_at": NOW.isoformat(), "streams": {}}}


def family(name, number=1, started=None, completed=None, missing=False):
    started = started or NOW - dt.timedelta(minutes=30 - number)
    completed = completed or started + dt.timedelta(minutes=1)
    clean = {"missing_from_right": [], "size_mismatch": [], "checksum_mismatch": []}
    result = {"verification_id": f"{name}-{number}", "verified_at": started.isoformat(),
              "evidence_started_at": started.isoformat(),
              "verification_completed_at": completed.isoformat(),
              "verification_scope": "full_family", "source_vs_s3": copy.deepcopy(clean),
              "source_vs_gws": copy.deepcopy(clean)}
    if missing:
        result["source_vs_s3"]["missing_from_right"] = ["missing.dat"]
    return result


def report(number=1, names=ALL_JOBS):
    return {"generated_at": (NOW - dt.timedelta(minutes=10 - number)).isoformat(),
            "verification_mode": "full", "verified_jobs": list(names),
            "jobs": {name: family(name, number) for name in names}}


def evaluate(payload, previous=None, cfg=None, now=NOW):
    raw = json.dumps(payload, sort_keys=True).encode()
    return gate.evaluate(cfg or config(), payload, previous or {},
                         report_sha256=gate.hashlib.sha256(raw).hexdigest(), now=now)


class FamilyEvidenceTests(unittest.TestCase):
    def test_two_distinct_complete_observations_per_family(self):
        first = evaluate(report())
        self.assertEqual(first["clean_streak"], 1)
        self.assertFalse(first["stable_parity"])
        replay = evaluate(report(), first)
        self.assertEqual(replay["clean_streak"], 1)
        second = evaluate(report(2), replay)
        self.assertTrue(second["stable_parity"])
        self.assertTrue(second["raw_retention_ready"])
        self.assertTrue(second["products_stable_parity"])

    def test_unrelated_family_cannot_increase_products_confirmation(self):
        value = report()
        state = evaluate(value)
        for number, name in enumerate(PRODUCT_JOBS[1:], 2):
            value["jobs"][name] = family(name, number)
            value["verified_jobs"] = [name]
            value.update(verification_mode="incremental", incremental_depth=number,
                         base_generated_at=state["last_generated_at"], base_report_sha256=state["report_sha256"])
            state = evaluate(value, state)
        self.assertEqual(state["families"]["products"]["clean_streak"], 1)
        self.assertEqual(state["domains"]["products"]["clean_streak"], 1)
        self.assertFalse(state["products_stable_parity"])
        self.assertEqual(state["families"]["raw"]["clean_streak"], 1)

    def test_replayed_older_epoch_never_adds_confirmation(self):
        state = evaluate(report())
        state = evaluate(report(2), state)
        state = evaluate(report(3), state)
        replay = evaluate(report(), state)
        self.assertEqual(replay["families"]["products"]["clean_streak"], 3)

    def test_dirty_family_resets_only_its_own_confirmations(self):
        state = evaluate(report(2), evaluate(report()))
        dirty = report(3)
        dirty["jobs"]["products"]["source_vs_s3"]["missing_from_right"] = ["lost.png"]
        state = evaluate(dirty, state)
        self.assertEqual(state["families"]["products"]["clean_streak"], 0)
        self.assertTrue(state["raw_retention_ready"])
        self.assertFalse(state["products_stable_parity"])
        state = evaluate(report(4), state)
        self.assertFalse(state["products_stable_parity"])
        state = evaluate(report(5), state)
        self.assertTrue(state["products_stable_parity"])

    def test_missing_configured_family_fails_its_domain_closed(self):
        value = report()
        del value["jobs"]["products-wxcam"]
        value["verified_jobs"].remove("products-wxcam")
        state = evaluate(value)
        self.assertIn("products-wxcam:configured_family_missing", state["failures"])
        self.assertFalse(state["domains"]["products"]["complete_verification"])
        self.assertTrue(state["domains"]["raw_retention"]["clean"])

    def test_empty_or_unconfigured_catalogue_is_not_clean(self):
        for cfg in ({"jobs": []}, {"jobs": [{"name": "raw"}]}, {"jobs": [{"name": "raw"}, {"name": "raw"}]}):
            with self.subTest(config=cfg):
                self.assertFalse(evaluate(report(), cfg=cfg)["clean"])

    def test_missing_scope_and_malformed_comparison_fail_closed(self):
        value = report()
        value["jobs"]["raw"]["verification_scope"] = "partial"
        value["jobs"]["raw"]["source_vs_s3"]["size_mismatch"] = None
        state = evaluate(value)
        self.assertIn("raw:verification_scope_not_full_family", state["failures"])
        self.assertIn("raw:size_mismatch_invalid", state["failures"])

    def test_policy_migration_never_inherits_aggregate_streak(self):
        old = {"policy_version": 6, "clean": True, "clean_streak": 71,
               "domains": {"raw_retention": {"clean_streak": 71}, "products": {"clean_streak": 71}}}
        state = evaluate(report(), old)
        self.assertTrue(state["clean"])
        self.assertFalse(state["stable_parity"])
        self.assertTrue(all(value["clean_streak"] == 1 for value in state["families"].values()))

    def test_legacy_family_timestamp_is_only_one_observation(self):
        value = report()
        for item in value["jobs"].values():
            for key in ("verification_id", "evidence_started_at", "verification_completed_at"):
                item.pop(key)
        state = evaluate(value)
        self.assertEqual(state["clean_streak"], 1)
        self.assertEqual(evaluate(value, state)["clean_streak"], 1)

    def test_same_report_expires_without_another_publication(self):
        value = report(2)
        state = evaluate(value, evaluate(report()))
        self.assertTrue(state["stable_parity"])
        later = evaluate(value, state, now=NOW + dt.timedelta(hours=8))
        self.assertFalse(later["raw_retention_ready"])
        self.assertTrue(later["products_stable_parity"])
        expired = evaluate(value, later, now=NOW + dt.timedelta(hours=36))
        self.assertFalse(expired["products_stable_parity"])

    def test_oldest_observation_controls_age_not_completion_or_report(self):
        value = report()
        start = NOW - dt.timedelta(hours=8, minutes=1)
        value["jobs"]["raw"] = family("raw", started=start, completed=start + dt.timedelta(hours=3))
        state = evaluate(value)
        self.assertFalse(state["raw_retention_ready"])
        self.assertEqual(state["domains"]["raw_retention"]["evidence_floor_generated_at"], start.isoformat())
        self.assertTrue(any(f.startswith("raw_retention_evidence_stale_hours=") for f in state["failures"]))

    def test_observation_windows_and_future_timestamps_fail_closed(self):
        for name, hours in (("raw", 5), ("products", 13)):
            value = report()
            value["jobs"][name] = family(name, started=NOW - dt.timedelta(hours=hours), completed=NOW)
            self.assertIn(f"{name}:verification_observation_window_exceeded", evaluate(value)["failures"])
        value["jobs"]["raw"] = family("raw", started=NOW, completed=NOW + dt.timedelta(hours=1))
        self.assertIn("raw:verification_observation_order_invalid", evaluate(value)["failures"])

    def test_independent_gws_age_and_required_counters_fail_closed(self):
        cfg = config()
        cfg["_gws_summary"]["generated_at"] = (NOW - dt.timedelta(hours=9)).isoformat()
        state = evaluate(report(), cfg=cfg)
        self.assertTrue(any(f.startswith("raw_retention_evidence_stale_hours=") for f in state["failures"]))
        cfg = config()
        cfg["streams"] = [{"name": "radar"}]
        self.assertIn("raw:gws_retention_gws_missing_count:radar=invalid", evaluate(report(), cfg=cfg)["failures"])
        cfg["_gws_summary"]["streams"]["radar"] = {field: 0 for field in (
            "retention_local_missing_count", "retention_local_mismatch_count",
            "retention_gws_missing_count", "retention_gws_mismatch_count")}
        self.assertTrue(evaluate(report(), cfg=cfg)["clean"])

    def test_incremental_base_is_hash_bound(self):
        first = evaluate(report())
        value = report(2)
        value.update(verification_mode="incremental", incremental_depth=1,
                     base_generated_at=first["last_generated_at"], base_report_sha256="wrong")
        self.assertIn("incremental_base_does_not_match_previous_report", evaluate(value, first)["failures"])
        value["base_report_sha256"] = first["report_sha256"]
        self.assertTrue(evaluate(value, first)["stable_parity"])

    def test_coalesced_checkpoint_trusts_hash_bound_history_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stamp = "2026-08-19T12:34:56.123456Z"
            path = root / "history" / gate.history_id(stamp) / "comparison.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(b'{"checkpoint": 1}\n')
            digest = gate.hashlib.sha256(path.read_bytes()).hexdigest()
            args = dict(manifest_root=root, base_generated_at=stamp, previous_generated_at="older", previous_report_sha256="older")
            self.assertTrue(gate.incremental_base_is_trusted(**args, expected_sha256=digest))
            self.assertFalse(gate.incremental_base_is_trusted(**args, expected_sha256="wrong"))

    def test_evaluate_does_not_read_files(self):
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("I/O")), mock.patch.object(Path, "read_bytes", side_effect=AssertionError("I/O")):
            self.assertTrue(evaluate(report())["clean"])

    def test_legacy_cli_rechecks_an_unchanged_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest = root / "latest"
            latest.mkdir()
            gws = root / "gws/latest"
            gws.mkdir(parents=True)
            now = dt.datetime.now(dt.timezone.utc)
            payload = report()
            for name in ALL_JOBS:
                payload["jobs"][name] = family(name, started=now - dt.timedelta(minutes=1), completed=now)
            payload["generated_at"] = now.isoformat()
            (latest / "comparison.json").write_text(json.dumps(payload))
            (gws / "summary.json").write_text(json.dumps({"generated_at": now.isoformat(), "streams": {}}))
            cfg = config()
            cfg.update(manifest_root=str(root), gws_manifest_root=str(root / "gws"))
            catalog, state = root / "catalog.json", root / "state.json"
            catalog.write_text(json.dumps(cfg))
            with mock.patch.object(gate, "CATALOG", catalog), mock.patch.object(gate, "STATE", state):
                self.assertEqual(gate.main(), 0)
                first = json.loads(state.read_text())
                self.assertEqual(gate.main(), 0)
                second = json.loads(state.read_text())
            self.assertEqual(first["clean_streak"], second["clean_streak"])
            self.assertNotEqual(first["evaluated_at"], second["evaluated_at"])


if __name__ == "__main__":
    unittest.main()
