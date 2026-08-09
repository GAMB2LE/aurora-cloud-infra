from __future__ import annotations

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


class ObjectStoreRecheckAfterRepairTests(unittest.TestCase):
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
            run = mock.Mock(
                side_effect=[
                    subprocess.CompletedProcess([], 1),
                    subprocess.CompletedProcess([], 0),
                ]
            )
            sleep = mock.Mock()

            result = recheck.recheck(
                result_path=result_path,
                report_path=report_path,
                inventory=Path("/inventory"),
                attempts=2,
                retry_delay_seconds=600,
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


if __name__ == "__main__":
    unittest.main()
