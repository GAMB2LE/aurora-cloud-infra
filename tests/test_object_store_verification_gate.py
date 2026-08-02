from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = (
    Path(__file__).parents[1]
    / "roles"
    / "object_store_mirror"
    / "files"
    / "aurora-object-store-verification-gate.py"
)
SPEC = importlib.util.spec_from_file_location("object_store_verification_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def report(generated_at: str, *, missing: bool = False) -> dict:
    return {
        "generated_at": generated_at,
        "jobs": {
            "raw": {
                "source_vs_s3": {
                    "missing_from_right": ["missing.nc"] if missing else [],
                    "size_mismatch": [],
                    "checksum_mismatch": [],
                },
                "source_vs_gws": {
                    "missing_from_right": [],
                    "size_mismatch": [],
                    "checksum_mismatch": [],
                },
            }
        },
    }


def write_gws_summary(root: Path) -> None:
    latest = root / "gws" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "summary.json").write_text(
        json.dumps({"streams": {}}), encoding="utf-8"
    )


class ObjectStoreVerificationGateTests(unittest.TestCase):
    def test_two_distinct_clean_reports_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest = root / "manifests" / "latest"
            latest.mkdir(parents=True)
            catalog = root / "catalog.json"
            state = root / "state.json"
            catalog.write_text(
                json.dumps(
                    {
                        "manifest_root": str(root / "manifests"),
                        "gws_manifest_root": str(root / "gws"),
                        "report_max_age_hours": 8,
                    }
                ),
                encoding="utf-8",
            )
            write_gws_summary(root)
            gate.CATALOG = catalog
            gate.STATE = state
            gate.REQUIRED = 2

            first = dt.datetime.now(dt.timezone.utc)
            (latest / "comparison.json").write_text(
                json.dumps(report(first.isoformat())),
                encoding="utf-8",
            )
            self.assertEqual(gate.main(), 0)
            first_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(first_state["clean_streak"], 1)
            self.assertFalse(first_state["stable_parity"])
            self.assertEqual(first_state["writers_policy"], "independent")

            second = first + dt.timedelta(seconds=1)
            (latest / "comparison.json").write_text(
                json.dumps(report(second.isoformat())),
                encoding="utf-8",
            )
            self.assertEqual(gate.main(), 0)
            second_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(second_state["clean_streak"], 2)
            self.assertTrue(second_state["stable_parity"])

    def test_incremental_repair_recheck_requires_a_clean_full_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest = root / "manifests" / "latest"
            latest.mkdir(parents=True)
            catalog = root / "catalog.json"
            state = root / "state.json"
            catalog.write_text(
                json.dumps(
                    {
                        "manifest_root": str(root / "manifests"),
                        "gws_manifest_root": str(root / "gws"),
                        "report_max_age_hours": 8,
                    }
                ),
                encoding="utf-8",
            )
            write_gws_summary(root)
            gate.CATALOG = catalog
            gate.STATE = state
            gate.REQUIRED = 2

            first = dt.datetime.now(dt.timezone.utc)
            dirty = report(first.isoformat(), missing=True)
            dirty.update(
                {
                    "verification_mode": "full",
                    "verified_jobs": ["raw"],
                    "evidence_floor_generated_at": first.isoformat(),
                }
            )
            (latest / "comparison.json").write_text(
                json.dumps(dirty), encoding="utf-8"
            )
            self.assertEqual(gate.main(), 0)

            second = first + dt.timedelta(seconds=1)
            repaired = report(second.isoformat())
            repaired.update(
                {
                    "verification_mode": "incremental",
                    "verified_jobs": ["raw"],
                    "base_generated_at": first.isoformat(),
                    "base_report_sha256": "abc123",
                    "evidence_floor_generated_at": first.isoformat(),
                    "incremental_depth": 1,
                }
            )
            (latest / "comparison.json").write_text(
                json.dumps(repaired), encoding="utf-8"
            )
            self.assertEqual(gate.main(), 0)
            repaired_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertTrue(repaired_state["clean"])
            self.assertEqual(repaired_state["clean_streak"], 1)
            self.assertEqual(repaired_state["full_clean_reports_in_streak"], 0)
            self.assertFalse(repaired_state["stable_parity"])

            third = second + dt.timedelta(seconds=1)
            incremental = report(third.isoformat())
            incremental.update(
                {
                    "verification_mode": "incremental",
                    "verified_jobs": ["raw"],
                    "base_generated_at": second.isoformat(),
                    "base_report_sha256": "def456",
                    "evidence_floor_generated_at": first.isoformat(),
                    "incremental_depth": 2,
                }
            )
            (latest / "comparison.json").write_text(
                json.dumps(incremental), encoding="utf-8"
            )
            self.assertEqual(gate.main(), 0)
            incremental_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(incremental_state["clean_streak"], 2)
            self.assertEqual(incremental_state["full_clean_reports_in_streak"], 0)
            self.assertFalse(incremental_state["stable_parity"])

            fourth = third + dt.timedelta(seconds=1)
            full = report(fourth.isoformat())
            full.update(
                {
                    "verification_mode": "full",
                    "verified_jobs": ["raw"],
                    "evidence_floor_generated_at": fourth.isoformat(),
                }
            )
            (latest / "comparison.json").write_text(
                json.dumps(full), encoding="utf-8"
            )
            self.assertEqual(gate.main(), 0)
            final_state = json.loads(state.read_text(encoding="utf-8"))

        self.assertEqual(final_state["clean_streak"], 3)
        self.assertEqual(final_state["full_clean_reports_in_streak"], 1)
        self.assertTrue(final_state["stable_parity"])

    def test_dirty_report_resets_clean_streak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest = root / "manifests" / "latest"
            latest.mkdir(parents=True)
            catalog = root / "catalog.json"
            state = root / "state.json"
            catalog.write_text(
                json.dumps(
                    {
                        "manifest_root": str(root / "manifests"),
                        "gws_manifest_root": str(root / "gws"),
                        "report_max_age_hours": 8,
                    }
                ),
                encoding="utf-8",
            )
            write_gws_summary(root)
            state.write_text(
                json.dumps({"last_generated_at": "old", "clean_streak": 1}),
                encoding="utf-8",
            )
            gate.CATALOG = catalog
            gate.STATE = state
            gate.REQUIRED = 2
            generated = dt.datetime.now(dt.timezone.utc).isoformat()
            (latest / "comparison.json").write_text(
                json.dumps(report(generated, missing=True)),
                encoding="utf-8",
            )

            self.assertEqual(gate.main(), 0)
            dirty = json.loads(state.read_text(encoding="utf-8"))

        self.assertEqual(dirty["clean_streak"], 0)
        self.assertFalse(dirty["stable_parity"])
        self.assertEqual(dirty["failures"], ["raw:missing_from_right=1"])

    def test_missing_gws_evidence_is_never_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest = root / "manifests" / "latest"
            latest.mkdir(parents=True)
            catalog = root / "catalog.json"
            state = root / "state.json"
            catalog.write_text(
                json.dumps(
                    {
                        "manifest_root": str(root / "manifests"),
                        "gws_manifest_root": str(root / "gws"),
                        "report_max_age_hours": 8,
                    }
                ),
                encoding="utf-8",
            )
            generated = dt.datetime.now(dt.timezone.utc).isoformat()
            incomplete = report(generated)
            del incomplete["jobs"]["raw"]["source_vs_gws"]
            (latest / "comparison.json").write_text(
                json.dumps(incomplete),
                encoding="utf-8",
            )
            gate.CATALOG = catalog
            gate.STATE = state
            gate.REQUIRED = 2

            self.assertEqual(gate.main(), 0)
            rejected = json.loads(state.read_text(encoding="utf-8"))

        self.assertFalse(rejected["clean"])
        self.assertEqual(rejected["clean_streak"], 0)
        self.assertFalse(rejected["stable_parity"])
        self.assertTrue(rejected["failures"][0].startswith("raw:gws_retention_evidence_unavailable="))


if __name__ == "__main__":
    unittest.main()
