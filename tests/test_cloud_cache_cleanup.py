from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = (
    Path(__file__).parents[1]
    / "roles"
    / "archive_dispatch"
    / "files"
    / "aurora-cloud-cache-cleanup.py"
)
SPEC = importlib.util.spec_from_file_location("cloud_cache_cleanup", SCRIPT)
assert SPEC and SPEC.loader
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


class CloudCacheCleanupTests(unittest.TestCase):
    def clean_health(self) -> dict:
        return {
            "overall_level": "green",
            "failures": [],
            "metrics": {
                "streams_gws_issue_count": 0,
                "object_store_raw_missing_count": 0,
                "object_store_raw_mismatch_count": 0,
            },
            "evidence": {
                "object_store_gate": {"stable_parity": True},
            },
        }

    def test_clean_archive_passes(self) -> None:
        cleanup.require_archive_clean(self.clean_health())

    def test_stale_or_incomplete_archive_is_rejected(self) -> None:
        health = self.clean_health()
        health["overall_level"] = "amber"
        health["failures"] = ["object_store_evidence_stale_hours=9"]

        with self.assertRaisesRegex(RuntimeError, "not green"):
            cleanup.require_archive_clean(health)

    def test_raw_gap_is_rejected(self) -> None:
        health = self.clean_health()
        health["metrics"]["object_store_raw_missing_count"] = 1

        with self.assertRaisesRegex(RuntimeError, "raw_missing_count=1"):
            cleanup.require_archive_clean(health)

    def test_staged_target_is_found_for_safe_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "wxcam.zarr"
            staged = Path(temporary) / "wxcam.zarr.deleting-run"
            staged.mkdir()

            self.assertEqual(cleanup.staged_targets(target), [staged])


if __name__ == "__main__":
    unittest.main()
