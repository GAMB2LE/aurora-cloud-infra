from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CL61AutomationShadowBoundaryTests(unittest.TestCase):
    def test_shadow_products_are_development_only_and_have_isolated_paths(self) -> None:
        variables = (ROOT / "inventory/group_vars/aurora_cloud.yml").read_text(encoding="utf-8")
        droplet = (ROOT / "inventory/host_vars/aurora-cloud-droplet.yml").read_text(encoding="utf-8")
        production = (ROOT / "inventory/host_vars/aurora-cloud.yml").read_text(encoding="utf-8")

        self.assertIn('aurora_cl61_automation_shadow_enabled: "{{ aurora_is_development }}"', variables)
        self.assertIn("aurora_cl61_automation_status_path", variables)
        self.assertIn("aurora_cl61_automation_api_status_path", variables)
        self.assertIn(
            "aurora_power_forecast_bundle_current_root ~ '/cl61_automation_status.json'",
            variables,
        )
        self.assertIn("aurora_cl61_automation_shadow_enabled: true", droplet)
        self.assertNotIn("aurora_cl61_automation_shadow_enabled: true", production)

    def test_operating_service_can_only_publish_diagnostic_shadow_intent(self) -> None:
        service = (
            ROOT
            / "roles/dashboard_services/templates/aurora-power-operating-scenarios.service.j2"
        ).read_text(encoding="utf-8")
        env = (ROOT / "roles/dashboard_services/templates/aurora-dashboard.env.j2").read_text(encoding="utf-8")
        tasks = (ROOT / "roles/dashboard_services/tasks/main.yml").read_text(encoding="utf-8")

        self.assertIn("--enable-automation-shadow", service)
        self.assertIn("aurora_cl61_automation_shadow_enabled", service)
        self.assertIn(
            "CL61_AUTOMATION_STATUS_PATH={{ aurora_cl61_automation_api_status_path }}",
            env,
        )
        self.assertIn(
            "--automation-status-output {{ aurora_cl61_automation_status_path }}",
            service,
        )
        self.assertIn("AURORA_CL61_AUTOMATION_SHADOW_ENABLED", env)
        self.assertIn("Assert CL61 shadow automation is development-only", tasks)
        self.assertIn("aurora_failover_role != 'primary'", tasks)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
