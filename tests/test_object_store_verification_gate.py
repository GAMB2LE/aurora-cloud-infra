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

PRODUCT_JOBS = (
    "products",
    "products-wxcam",
    "model-evaluation",
    "menapia-flight-manifests",
    "manifests",
)


def clean_comparison(*, missing: bool = False) -> dict:
    return {
        "missing_from_right": ["quicklook.png"] if missing else [],
        "size_mismatch": [],
        "checksum_mismatch": [],
    }


def report(generated_at: str, *, missing: bool = False) -> dict:
    return {
        "generated_at": generated_at,
        "jobs": {
            "raw": {
                "verified_at": generated_at,
                "verification_scope": "full_family",
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


def report_with_products(generated_at: str, *, product_missing: bool) -> dict:
    value = report(generated_at)
    value["jobs"]["products"] = {
        "verified_at": generated_at,
        "verification_scope": "full_family",
        "source_vs_s3": {
            "missing_from_right": ["quicklook.png"] if product_missing else [],
            "size_mismatch": [],
            "checksum_mismatch": [],
        },
        "source_vs_gws": {
            "missing_from_right": [],
            "size_mismatch": [],
            "checksum_mismatch": [],
        },
    }
    return value


def report_with_all_products(
    generated_at: str,
    *,
    product_verified_at: str | None = None,
    product_missing: bool = False,
) -> dict:
    value = report(generated_at)
    for name in PRODUCT_JOBS:
        value["jobs"][name] = {
            "verified_at": product_verified_at or generated_at,
            "verification_scope": "full_family",
            "source_vs_s3": clean_comparison(
                missing=product_missing and name == "products"
            ),
            "source_vs_gws": clean_comparison(),
        }
    return value


def write_gws_summary(root: Path) -> None:
    latest = root / "gws" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "summary.json").write_text(
        json.dumps(
            {
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "streams": {},
            }
        ),
        encoding="utf-8",
    )


class ObjectStoreVerificationGateTests(unittest.TestCase):
    def configure_gate(
        self,
        root: Path,
        *,
        domain_max_age: dict[str, int] | None = None,
    ) -> tuple[Path, Path]:
        latest = root / "manifests" / "latest"
        latest.mkdir(parents=True)
        catalog = root / "catalog.json"
        state = root / "state.json"
        config = {
            "manifest_root": str(root / "manifests"),
            "gws_manifest_root": str(root / "gws"),
            "report_max_age_hours": 8,
        }
        if domain_max_age is not None:
            config["domain_evidence_max_age_hours"] = domain_max_age
        catalog.write_text(json.dumps(config), encoding="utf-8")
        write_gws_summary(root)
        gate.CATALOG = catalog
        gate.STATE = state
        gate.REQUIRED = 2
        return latest, state

    def test_coalesced_checkpoint_trusts_hash_bound_history_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated_at = "2026-08-19T12:34:56.123456Z"
            snapshot = (
                root
                / "history"
                / gate.history_id(generated_at)
                / "comparison.json"
            )
            snapshot.parent.mkdir(parents=True)
            snapshot.write_bytes(b'{"checkpoint": 1}\n')
            digest = gate.hashlib.sha256(snapshot.read_bytes()).hexdigest()

            self.assertTrue(
                gate.incremental_base_is_trusted(
                    manifest_root=root,
                    base_generated_at=generated_at,
                    expected_sha256=digest,
                    previous_generated_at="2026-08-19T08:00:00Z",
                    previous_report_sha256="different-report",
                )
            )
            self.assertFalse(
                gate.incremental_base_is_trusted(
                    manifest_root=root,
                    base_generated_at=generated_at,
                    expected_sha256="not-the-snapshot-hash",
                    previous_generated_at="2026-08-19T08:00:00Z",
                    previous_report_sha256="different-report",
                )
            )

    def test_stale_canonical_gws_summary_blocks_raw_retention(self) -> None:
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
            gws_latest = root / "gws" / "latest"
            gws_latest.mkdir(parents=True)
            old = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
            ).isoformat()
            (gws_latest / "summary.json").write_text(
                json.dumps({"generated_at": old, "streams": {}}),
                encoding="utf-8",
            )
            generated = dt.datetime.now(dt.timezone.utc).isoformat()
            payload = report(generated)
            payload.update(
                {
                    "verification_mode": "full",
                    "verified_jobs": ["raw"],
                    "evidence_floor_generated_at": generated,
                }
            )
            (latest / "comparison.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            gate.CATALOG = catalog
            gate.STATE = state

            self.assertEqual(gate.main(), 0)
            result = json.loads(state.read_text(encoding="utf-8"))

        self.assertFalse(result["raw_retention_ready"])
        self.assertTrue(
            any(
                failure.startswith("raw_retention_evidence_stale_hours=")
                for failure in result["domains"]["raw_retention"]["failures"]
            )
        )

    def test_fresh_raw_recheck_is_independent_of_stale_products(self) -> None:
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
            now = dt.datetime.now(dt.timezone.utc)
            old = (now - dt.timedelta(days=2)).isoformat()
            base_generated_at = (now - dt.timedelta(hours=4)).isoformat()
            state.write_text(
                json.dumps(
                    {
                        "policy_version": gate.POLICY_VERSION,
                        "last_generated_at": base_generated_at,
                        "report_sha256": "abc123",
                        "domains": {
                            "raw_retention": {
                                "clean": True,
                                "clean_streak": 1,
                                "full_clean_reports_in_streak": 1,
                                "evidence_id": "previous-raw",
                            },
                            "products": {
                                "clean": True,
                                "clean_streak": 1,
                                "full_clean_reports_in_streak": 1,
                                "evidence_id": "previous-products",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            payload = report_with_products(
                now.isoformat(), product_missing=False
            )
            payload["jobs"]["products"]["verified_at"] = old
            payload.update(
                {
                    "verification_mode": "incremental",
                    "verified_jobs": ["raw"],
                    "base_generated_at": base_generated_at,
                    "base_report_sha256": "abc123",
                    "incremental_depth": 99,
                    "evidence_floor_generated_at": old,
                }
            )
            (latest / "comparison.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            gate.CATALOG = catalog
            gate.STATE = state
            gate.REQUIRED = 2

            self.assertEqual(gate.main(), 0)
            result = json.loads(state.read_text(encoding="utf-8"))

        self.assertTrue(result["raw_retention_ready"])
        self.assertFalse(result["products_stable_parity"])
        self.assertEqual(result["domains"]["raw_retention"]["failures"], [])
        self.assertTrue(
            any(
                failure.startswith("products_evidence_stale_hours=")
                for failure in result["domains"]["products"]["failures"]
            )
        )

    def test_domain_horizons_keep_products_current_without_relaxing_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest, state = self.configure_gate(
                root,
                domain_max_age={"raw_retention": 8, "products": 36},
            )
            now = dt.datetime.now(dt.timezone.utc)
            payload = report_with_all_products(
                now.isoformat(),
                product_verified_at=(now - dt.timedelta(hours=14)).isoformat(),
            )
            payload["jobs"]["raw"]["verified_at"] = (
                now - dt.timedelta(hours=9)
            ).isoformat()
            payload.update(
                {
                    "verification_mode": "full",
                    "verified_jobs": ["raw", *PRODUCT_JOBS],
                    "evidence_floor_generated_at": (
                        now - dt.timedelta(hours=14)
                    ).isoformat(),
                }
            )
            (latest / "comparison.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            self.assertEqual(gate.main(), 0)
            result = json.loads(state.read_text(encoding="utf-8"))

        self.assertFalse(result["raw_retention_ready"])
        self.assertTrue(result["domains"]["products"]["clean"])
        self.assertEqual(
            result["domains"]["raw_retention"]["evidence_max_age_hours"],
            8,
        )
        self.assertEqual(
            result["domains"]["products"]["evidence_max_age_hours"],
            36,
        )
        self.assertTrue(
            any(
                failure.startswith("raw_retention_evidence_stale_hours=")
                for failure in result["domains"]["raw_retention"]["failures"]
            )
        )
        self.assertEqual(result["domains"]["products"]["failures"], [])

    def test_products_evidence_still_expires_at_its_domain_horizon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest, state = self.configure_gate(
                root,
                domain_max_age={"raw_retention": 8, "products": 36},
            )
            now = dt.datetime.now(dt.timezone.utc)
            old = (now - dt.timedelta(hours=37)).isoformat()
            payload = report_with_all_products(
                now.isoformat(), product_verified_at=old
            )
            payload.update(
                {
                    "verification_mode": "full",
                    "verified_jobs": ["raw", *PRODUCT_JOBS],
                    "evidence_floor_generated_at": old,
                }
            )
            (latest / "comparison.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            self.assertEqual(gate.main(), 0)
            result = json.loads(state.read_text(encoding="utf-8"))

        self.assertTrue(result["domains"]["raw_retention"]["clean"])
        self.assertFalse(result["products_stable_parity"])
        self.assertTrue(
            any(
                failure.startswith("products_evidence_stale_hours=")
                for failure in result["domains"]["products"]["failures"]
            )
        )

    def test_product_rechecks_restore_complete_composite_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest, state = self.configure_gate(
                root,
                domain_max_age={"raw_retention": 8, "products": 36},
            )
            first = dt.datetime.now(dt.timezone.utc)
            dirty = report_with_all_products(
                first.isoformat(), product_missing=True
            )
            dirty.update(
                {
                    "verification_mode": "full",
                    "verified_jobs": ["raw", *PRODUCT_JOBS],
                    "evidence_floor_generated_at": first.isoformat(),
                }
            )
            report_path = latest / "comparison.json"
            report_path.write_text(json.dumps(dirty), encoding="utf-8")
            self.assertEqual(gate.main(), 0)
            dirty_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(
                dirty_state["domains"]["products"][
                    "full_clean_reports_in_streak"
                ],
                0,
            )

            second = first + dt.timedelta(seconds=1)
            repaired = report_with_all_products(first.isoformat())
            repaired["jobs"]["products"]["verified_at"] = second.isoformat()
            repaired.update(
                {
                    "generated_at": second.isoformat(),
                    "verification_mode": "incremental",
                    "verified_jobs": ["products"],
                    "base_generated_at": first.isoformat(),
                    "base_report_sha256": dirty_state["report_sha256"],
                    "evidence_floor_generated_at": first.isoformat(),
                    "incremental_depth": 1,
                }
            )
            report_path.write_text(json.dumps(repaired), encoding="utf-8")
            self.assertEqual(gate.main(), 0)
            repaired_state = json.loads(state.read_text(encoding="utf-8"))
            repaired_products = repaired_state["domains"]["products"]
            self.assertEqual(repaired_products["clean_streak"], 1)
            self.assertEqual(
                repaired_products["full_clean_reports_in_streak"], 1
            )
            self.assertTrue(repaired_products["complete_verification"])
            self.assertFalse(repaired_products["stable_parity"])

            third = second + dt.timedelta(seconds=1)
            confirmed = json.loads(json.dumps(repaired))
            confirmed["generated_at"] = third.isoformat()
            confirmed["jobs"]["products"]["verified_at"] = third.isoformat()
            confirmed["base_generated_at"] = second.isoformat()
            confirmed["base_report_sha256"] = repaired_state["report_sha256"]
            confirmed["incremental_depth"] = 2
            report_path.write_text(json.dumps(confirmed), encoding="utf-8")
            self.assertEqual(gate.main(), 0)
            confirmed_state = json.loads(state.read_text(encoding="utf-8"))
            confirmed_products = confirmed_state["domains"]["products"]
            self.assertEqual(confirmed_products["clean_streak"], 2)
            self.assertEqual(
                confirmed_products["full_clean_reports_in_streak"], 2
            )
            self.assertTrue(confirmed_products["stable_parity"])

            fourth = third + dt.timedelta(seconds=1)
            reused = json.loads(json.dumps(confirmed))
            reused["generated_at"] = fourth.isoformat()
            reused["jobs"]["raw"]["verified_at"] = fourth.isoformat()
            reused["verified_jobs"] = ["raw"]
            reused["base_generated_at"] = third.isoformat()
            reused["base_report_sha256"] = confirmed_state["report_sha256"]
            reused["incremental_depth"] = 3
            report_path.write_text(json.dumps(reused), encoding="utf-8")
            self.assertEqual(gate.main(), 0)
            reused_state = json.loads(state.read_text(encoding="utf-8"))

        self.assertEqual(
            reused_state["domains"]["products"]["clean_streak"], 2
        )
        self.assertEqual(
            reused_state["domains"]["products"][
                "full_clean_reports_in_streak"
            ],
            2,
        )

    def test_partial_current_family_scope_cannot_remain_stable(self) -> None:
        previous = {
            "clean": True,
            "clean_streak": 1,
            "full_clean_reports_in_streak": 1,
            "evidence_id": "complete-1",
            "last_clean_at": "2026-09-04T12:00:00+00:00",
        }

        result = gate.build_domain_state(
            previous=previous,
            failures=[],
            mode="incremental",
            generated_at="2026-09-04T12:01:00+00:00",
            evidence_floor="2026-09-04T12:00:00+00:00",
            evidence_id="partial-2",
            evidence_max_age_hours=36,
            verified=True,
            complete_verification=False,
        )

        self.assertEqual(result["clean_streak"], 2)
        self.assertEqual(result["full_clean_reports_in_streak"], 1)
        self.assertFalse(result["complete_verification"])
        self.assertFalse(result["stable_parity"])

    def test_malformed_family_evidence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest, state = self.configure_gate(root)
            generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
            payload = report(generated_at)
            del payload["jobs"]["raw"]["verification_scope"]
            payload["jobs"]["raw"]["source_vs_s3"] = {}
            payload.update(
                {
                    "verification_mode": "full",
                    "verified_jobs": ["raw"],
                    "evidence_floor_generated_at": generated_at,
                }
            )
            (latest / "comparison.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            self.assertEqual(gate.main(), 0)
            result = json.loads(state.read_text(encoding="utf-8"))

        self.assertFalse(result["raw_retention_ready"])
        self.assertIn(
            "raw:verification_scope_not_full_family",
            result["domains"]["raw_retention"]["failures"],
        )
        self.assertIn(
            "raw:missing_from_right_invalid",
            result["domains"]["raw_retention"]["failures"],
        )

    def test_product_gap_does_not_reset_raw_retention_domain(self) -> None:
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
            for offset in (0, 1):
                payload = report_with_products(
                    (first + dt.timedelta(seconds=offset)).isoformat(),
                    product_missing=True,
                )
                payload.update(
                    {
                        "verification_mode": "full",
                        "verified_jobs": ["raw", "products"],
                        "evidence_floor_generated_at": payload["generated_at"],
                    }
                )
                (latest / "comparison.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                self.assertEqual(gate.main(), 0)

            result = json.loads(state.read_text(encoding="utf-8"))

        self.assertTrue(result["raw_retention_ready"])
        self.assertTrue(result["domains"]["raw_retention"]["stable_parity"])
        self.assertFalse(result["domains"]["products"]["stable_parity"])
        self.assertFalse(result["stable_parity"])

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

    def test_two_complete_raw_family_rechecks_restore_raw_parity(self) -> None:
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
            dirty_state = json.loads(state.read_text(encoding="utf-8"))

            second = first + dt.timedelta(seconds=1)
            repaired = report(second.isoformat())
            repaired.update(
                {
                    "verification_mode": "incremental",
                    "verified_jobs": ["raw"],
                    "base_generated_at": first.isoformat(),
                    "base_report_sha256": dirty_state["report_sha256"],
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
            self.assertEqual(repaired_state["full_clean_reports_in_streak"], 1)
            self.assertFalse(repaired_state["stable_parity"])

            third = second + dt.timedelta(seconds=1)
            incremental = report(third.isoformat())
            incremental.update(
                {
                    "verification_mode": "incremental",
                    "verified_jobs": ["raw"],
                    "base_generated_at": second.isoformat(),
                    "base_report_sha256": repaired_state["report_sha256"],
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
            self.assertEqual(incremental_state["full_clean_reports_in_streak"], 2)
            self.assertTrue(incremental_state["stable_parity"])

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
        self.assertEqual(final_state["full_clean_reports_in_streak"], 3)
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
