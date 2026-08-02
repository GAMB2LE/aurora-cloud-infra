from pathlib import Path
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

    def test_first_clean_audit_is_amber_while_confirmation_is_pending(self):
        result = operator_status(
            ["object_store_stable_parity=false"],
            self.base_metrics(),
            {"clean": True, "stable_parity": False},
            {"state": "complete"},
        )

        self.assertEqual(result["level"], "amber")
        self.assertEqual(result["title"], "Archive parity is being confirmed")
        self.assertIn("One complete clean strict audit", result["detail"])
        self.assertTrue(result["pruning_paused"])

    def test_incremental_repair_recheck_is_explained(self):
        result = operator_status(
            ["object_store_stable_parity=false"],
            self.base_metrics(),
            {
                "clean": True,
                "stable_parity": False,
                "verification_mode": "incremental",
            },
            {"state": "complete"},
        )

        self.assertEqual(result["level"], "amber")
        self.assertIn("exact-path repair recheck", result["detail"])
        self.assertIn("complete clean strict audit", result["detail"])


if __name__ == "__main__":
    unittest.main()
