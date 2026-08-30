from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "roles/dashboard_services/templates/aurora-power-v12-candidate.service.j2"
ENVIRONMENT = ROOT / "roles/dashboard_services/templates/aurora-dashboard.env.j2"


class V12CandidateEnsembleBoundaryTests(unittest.TestCase):
    def test_memberwise_baseline_is_explicitly_read_only_and_optional(self) -> None:
        service = SERVICE.read_text(encoding="utf-8")
        environment = ENVIRONMENT.read_text(encoding="utf-8")

        self.assertIn(
            "--baseline-ensemble-zarr {{ aurora_zarr.power_soc_ensemble }}", service
        )
        self.assertIn(
            "AURORA_POWER_BASELINE_ENSEMBLE_ZARR={{ aurora_zarr.power_soc_ensemble }}",
            environment,
        )
        self.assertNotIn("ConditionPathExists={{ aurora_zarr.power_soc_ensemble }}", service)
        self.assertIn("ReadWritePaths={{ aurora_power_v12_candidate_root }}", service)
        self.assertIn("MemoryMax=1.5G", service)


if __name__ == "__main__":
    unittest.main()
