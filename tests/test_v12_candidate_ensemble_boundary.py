from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "roles/dashboard_services/templates/aurora-power-v12-candidate.service.j2"
ENVIRONMENT = ROOT / "roles/dashboard_services/templates/aurora-dashboard.env.j2"
LAUNCHER = ROOT / "roles/dashboard_services/templates/aurora-power-v12-candidate-launch.py.j2"


class V12CandidateEnsembleBoundaryTests(unittest.TestCase):
    def test_memberwise_baseline_is_bound_to_the_immutable_independent_bundle(self) -> None:
        service = SERVICE.read_text(encoding="utf-8")
        environment = ENVIRONMENT.read_text(encoding="utf-8")
        launcher = LAUNCHER.read_text(encoding="utf-8")

        self.assertIn("aurora_power_forecast_bundle_independent_root", service)
        self.assertIn('"--baseline-ensemble-zarr", str(resolved["ensemble"])', launcher)
        self.assertIn('manifest.get("independentCycle") is not True', launcher)
        self.assertIn(
            "AURORA_POWER_BASELINE_ENSEMBLE_ZARR={{ aurora_zarr.power_soc_ensemble }}",
            environment,
        )
        self.assertNotIn("ConditionPathExists={{ aurora_zarr.power_soc_ensemble }}", service)
        self.assertIn("ReadWritePaths={{ aurora_power_v12_candidate_root }}", service)
        self.assertIn("MemoryMax=1.5G", service)
        self.assertIn(
            "ExecCondition={{ aurora_venv }}/bin/python /usr/local/bin/aurora-power-v12-candidate-launch --require-model-evaluation-idle",
            service,
        )
        self.assertLess(service.index("ExecCondition="), service.index("ExecStart="))
        self.assertIn('MODEL_EVALUATION_UNIT = "aurora-model-evaluation-daily.service"', launcher)
        self.assertIn('active_state not in {"inactive", "failed"}', launcher)
        self.assertIn('reason_code="model_evaluation_active"', launcher)
        self.assertIn('["--require-model-evaluation-idle"]', launcher)


if __name__ == "__main__":
    unittest.main()
