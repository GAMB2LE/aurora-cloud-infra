from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = (
    Path(__file__).parents[1]
    / "roles"
    / "object_store_mirror"
    / "files"
    / "aurora-object-store-recheck-after-repair.py"
)
SPEC = importlib.util.spec_from_file_location("object_store_recheck", SCRIPT)
assert SPEC and SPEC.loader
recheck = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recheck)


def publish_confirmation(
    report_path: Path,
    gate_path: Path,
    *,
    generated_at: str,
    full_clean_reports: int,
    missing: bool = False,
    gate_clean: bool = True,
) -> None:
    report = {
        "generated_at": generated_at,
        "verification_mode": "incremental",
        "verified_jobs": ["products"],
        "jobs": {
            "products": {
                "verified_at": generated_at,
                "verification_scope": "full_family",
                "source_vs_s3": {
                    "missing_from_right": ["quicklook.png"] if missing else [],
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
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
    gate_path.write_text(
        json.dumps(
            {
                "last_generated_at": generated_at,
                "report_sha256": report_sha256,
                "domains": {
                    "products": {
                        "clean": gate_clean,
                        "verified_in_report": True,
                        "complete_verification": True,
                        "full_clean_reports_in_streak": full_clean_reports,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


class ObjectStoreRecheckAfterRepairTests(unittest.TestCase):
    def test_legacy_exit_only_confirmation_state_is_not_trusted(self) -> None:
        legacy = {
            "schema_version": 1,
            "repair_report": "report-1",
            "jobs": ["products"],
            "confirmations": 2,
        }
        current = {
            "schema_version": 2,
            "repair_report": "report-1",
            "jobs": ["products"],
            "confirmations": 2,
            "confirmation_reports": [
                {"generated_at": "report-2", "report_sha256": "a" * 64},
                {"generated_at": "report-3", "report_sha256": "b" * 64},
            ],
        }

        self.assertFalse(
            recheck.confirmation_state_is_trusted(
                legacy,
                repair_report="report-1",
                jobs=["products"],
                required_confirmations=2,
            )
        )
        self.assertTrue(
            recheck.confirmation_state_is_trusted(
                current,
                repair_report="report-1",
                jobs=["products"],
                required_confirmations=2,
            )
        )

    def test_only_successfully_copied_jobs_are_selected(self) -> None:
        result = {
            "report": "report-1",
            "jobs": [
                {"job": "products", "ready": 1, "returncode": 0},
                {"job": "raw", "ready": 0, "returncode": 0},
                {"job": "manifests", "ready": 1, "returncode": 1},
            ],
        }
        report = {
            "generated_at": "report-1",
            "jobs": {"products": {}, "raw": {}, "manifests": {}},
        }

        self.assertEqual(recheck.repaired_jobs(result, report), ["products"])

    def test_stale_repair_result_does_not_recheck_new_report(self) -> None:
        result = {
            "report": "old",
            "jobs": [{"job": "products", "ready": 1, "returncode": 0}],
        }
        report = {"generated_at": "new", "jobs": {"products": {}}}

        self.assertEqual(recheck.repaired_jobs(result, report), [])

    def test_failed_incremental_inventory_retries_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / "result.json"
            report_path = root / "comparison.json"
            gate_path = root / "gate.json"
            result_path.write_text(
                json.dumps(
                    {
                        "report": "report-1",
                        "jobs": [
                            {"job": "products", "ready": 1, "returncode": 0}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_path.write_text(
                json.dumps(
                    {"generated_at": "report-1", "jobs": {"products": {}}}
                ),
                encoding="utf-8",
            )
            calls = 0

            def inventory_run(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return subprocess.CompletedProcess([], 1)
                publish_confirmation(
                    report_path,
                    gate_path,
                    generated_at="report-2",
                    full_clean_reports=1,
                )
                return subprocess.CompletedProcess([], 0)

            run = mock.Mock(side_effect=inventory_run)
            sleep = mock.Mock()

            result = recheck.recheck(
                result_path=result_path,
                report_path=report_path,
                inventory=Path("/inventory"),
                attempts=2,
                retry_delay_seconds=600,
                gate_state_path=gate_path,
                gate_wait_seconds=0,
                run=run,
                sleep=sleep,
            )

        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(600)
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["/inventory", "--reuse-latest", "--job", "products"],
        )

    def test_two_clean_confirmations_are_recorded_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / "result.json"
            report_path = root / "comparison.json"
            state_path = root / "state.json"
            gate_path = root / "gate.json"
            result_path.write_text(
                json.dumps(
                    {
                        "report": "report-1",
                        "jobs": [
                            {"job": "products", "ready": 1, "returncode": 0}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_path.write_text(
                json.dumps(
                    {"generated_at": "report-1", "jobs": {"products": {}}}
                ),
                encoding="utf-8",
            )
            calls = 0

            def inventory_run(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                publish_confirmation(
                    report_path,
                    gate_path,
                    generated_at=f"report-{calls + 1}",
                    full_clean_reports=calls,
                )
                return subprocess.CompletedProcess([], 0)

            run = mock.Mock(side_effect=inventory_run)
            sleep = mock.Mock()

            self.assertEqual(
                recheck.recheck(
                    result_path=result_path,
                    report_path=report_path,
                    inventory=Path("/inventory"),
                    attempts=2,
                    retry_delay_seconds=30,
                    confirmations=2,
                    confirmation_delay_seconds=600,
                    state_path=state_path,
                    gate_state_path=gate_path,
                    gate_wait_seconds=0,
                    run=run,
                    sleep=sleep,
                ),
                0,
            )
            self.assertEqual(run.call_count, 2)
            sleep.assert_called_once_with(600)

            self.assertEqual(
                recheck.recheck(
                    result_path=result_path,
                    report_path=report_path,
                    inventory=Path("/inventory"),
                    attempts=2,
                    retry_delay_seconds=30,
                    confirmations=2,
                    confirmation_delay_seconds=600,
                    state_path=state_path,
                    gate_state_path=gate_path,
                    gate_wait_seconds=0,
                    run=run,
                    sleep=sleep,
                ),
                0,
            )
            self.assertEqual(run.call_count, 2)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["schema_version"], 2)
            self.assertEqual(len(state["confirmation_reports"]), 2)

    def test_zero_exit_with_a_dirty_report_is_not_a_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / "result.json"
            report_path = root / "comparison.json"
            gate_path = root / "gate.json"
            state_path = root / "state.json"
            result_path.write_text(
                json.dumps(
                    {
                        "report": "report-1",
                        "jobs": [
                            {"job": "products", "ready": 1, "returncode": 0}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_path.write_text(
                json.dumps(
                    {"generated_at": "report-1", "jobs": {"products": {}}}
                ),
                encoding="utf-8",
            )

            def inventory_run(*_args, **_kwargs):
                publish_confirmation(
                    report_path,
                    gate_path,
                    generated_at="report-2",
                    full_clean_reports=0,
                    missing=True,
                    gate_clean=False,
                )
                return subprocess.CompletedProcess([], 0)

            result = recheck.recheck(
                result_path=result_path,
                report_path=report_path,
                inventory=Path("/inventory"),
                attempts=1,
                retry_delay_seconds=0,
                confirmations=1,
                state_path=state_path,
                gate_state_path=gate_path,
                gate_wait_seconds=0,
                run=inventory_run,
                sleep=lambda _seconds: None,
            )
            state_exists = state_path.exists()

        self.assertEqual(result, 1)
        self.assertFalse(state_exists)

    def test_clean_report_requires_the_exact_gate_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "comparison.json"
            gate_path = root / "gate.json"
            publish_confirmation(
                report_path,
                gate_path,
                generated_at="report-2",
                full_clean_reports=1,
                gate_clean=False,
            )
            report, report_sha256 = recheck.load_clean_confirmation(
                report_path,
                ["products"],
                "report-1",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "verification gate rejected the products confirmation",
            ):
                recheck.wait_for_gate_confirmation(
                    report_path=report_path,
                    gate_state_path=gate_path,
                    report=report,
                    report_sha256=report_sha256,
                    jobs=["products"],
                    minimum_full_clean=1,
                    wait_seconds=0,
                    sleep=lambda _seconds: None,
                )

    def test_malformed_comparison_cannot_be_a_clean_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "comparison.json"
            gate_path = Path(tmp) / "gate.json"
            publish_confirmation(
                report_path,
                gate_path,
                generated_at="report-2",
                full_clean_reports=1,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["jobs"]["products"]["source_vs_s3"] = {}
            report_path.write_text(json.dumps(report), encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError,
                "kept a settled gap for products:source_vs_s3",
            ):
                recheck.load_clean_confirmation(
                    report_path,
                    ["products"],
                    "report-1",
                )


if __name__ == "__main__":
    unittest.main()
