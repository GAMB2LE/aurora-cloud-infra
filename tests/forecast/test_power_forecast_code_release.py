"""The focused release must preserve commissioned commands and writer boundaries."""

from pathlib import Path
import re

from jinja2 import Environment, StrictUndefined
import yaml


ROOT = Path(__file__).resolve().parents[2]
PLAY = yaml.safe_load((ROOT / "playbooks/power_forecast_code_release.yml").read_text())[0]
TEMPLATES = ROOT / "roles/dashboard_services/templates"
SCOPED_TEMPLATES = {
    "aurora-power-soc-forecast.service.j2",
    "aurora-power-soc-forecast-learn.service.j2",
    "aurora-power-soc-ensemble.service.j2",
    "aurora-power-soc-planning-forecast.service.j2",
    "aurora-power-operating-scenarios.service.j2",
    "aurora-power-forecast-publish.service.j2",
    "aurora-power-forecast-bundle-failure@.service.j2",
    "aurora-power-forecast-runner.sh.j2",
    "aurora-power-forecast-publish.py.j2",
}


def _select_paths(text: str) -> str:
    env = Environment(undefined=StrictUndefined)
    env.filters["regex_escape"] = re.escape
    context = {
        "power_forecast_previous_app_dir": "/opt/aurora-cloud-dashboard",
        "power_forecast_previous_venv": "/opt/aurora-cloud-dashboard/venv",
        "aurora_power_forecast_app_dir": "/opt/aurora-presentations/" + "a" * 40,
        "aurora_power_forecast_venv": "/opt/aurora-presentations/runtime-reviewed",
    }
    for task in PLAY["tasks"]:
        if "ansible.builtin.replace" not in task:
            continue
        operation = task["ansible.builtin.replace"]
        assert operation["backup"] is True
        text = re.sub(
            env.from_string(operation["regexp"]).render(**context),
            env.from_string(operation["replace"]).render(**context),
            text,
        )
    return text


def test_path_selection_preserves_live_arguments_sandbox_and_is_idempotent():
    # Includes both production planning fallback commands and the development
    # wrapper's independent Python/source declarations, plus adjacent names.
    before = '''[Service]
User=aurora
WorkingDirectory=/opt/aurora-cloud-dashboard
ExecStart=/bin/bash -lc 'if /opt/aurora-cloud-dashboard/venv/bin/python generate.py --output-zarr /unchanged/forecast.zarr; then exit 0; fi; /opt/aurora-cloud-dashboard/venv/bin/python generate.py --refresh-from-cache'
TimeoutStartSec=1600
ReadOnlyPaths=/opt/aurora-cloud-dashboard /data/aurora/products /project/aurora/raw
ReadWritePaths=/data/aurora/dev-products/power
APP_DIR = Path("/opt/aurora-cloud-dashboard")
PYTHON = Path("/opt/aurora-cloud-dashboard/venv/bin/python")
#!/opt/aurora-cloud-dashboard/venv/bin/python
unrelated=/opt/aurora-cloud-dashboard-other /tmp/opt/aurora-cloud-dashboard
'''
    after = _select_paths(before)
    assert "/opt/aurora-presentations/runtime-reviewed/bin/python" in after
    assert "/opt/aurora-presentations/" + "a" * 40 + "/venv" not in after
    assert _select_paths(after) == after
    restored = after.replace("/opt/aurora-presentations/runtime-reviewed", "/opt/aurora-cloud-dashboard/venv")
    restored = restored.replace("/opt/aurora-presentations/" + "a" * 40, "/opt/aurora-cloud-dashboard")
    assert restored == before
    assert "unrelated=/opt/aurora-cloud-dashboard-other /tmp/opt/aurora-cloud-dashboard" in after


def test_only_forecast_templates_use_the_independent_pins():
    users = {
        path.name for path in TEMPLATES.glob("*.j2")
        if "aurora_power_forecast_app_dir" in path.read_text()
        or "aurora_power_forecast_venv" in path.read_text()
    }
    assert users == SCOPED_TEMPLATES
    for name in users:
        text = (TEMPLATES / name).read_text()
        assert "aurora_app_dir" not in text
        assert "aurora_venv" not in text
    defaults = yaml.safe_load((ROOT / "inventory/group_vars/aurora_cloud.yml").read_text())
    assert defaults["aurora_power_forecast_app_dir"] == "{{ aurora_app_dir }}"
    assert defaults["aurora_power_forecast_venv"] == "{{ aurora_venv }}"


def test_release_does_not_retemplate_units_start_jobs_or_touch_timers():
    assert "roles" not in PLAY
    services = PLAY["vars"]["power_forecast_services"] + PLAY["vars"]["power_forecast_orchestration_services"]
    assert {name + ".j2" for name in services} == {name for name in SCOPED_TEMPLATES if name.endswith("service.j2")}
    assert all(name.endswith(".service") for name in services)
    all_tasks = PLAY["tasks"] + PLAY["handlers"]
    assert not any("ansible.builtin.template" in task for task in all_tasks)
    systemd_tasks = [task["ansible.builtin.systemd_service"] for task in all_tasks if "ansible.builtin.systemd_service" in task]
    assert systemd_tasks == [{"daemon_reload": True}]
    commands = [task["ansible.builtin.command"]["argv"] for task in all_tasks if "ansible.builtin.command" in task]
    assert not any(isinstance(argv, list) and argv[:2] in (["systemctl", "start"], ["systemctl", "restart"]) for argv in commands)
    idle = next(task for task in PLAY["tasks"] if task["name"] == "Require forecast producers to be idle for code selection")
    first_mutation = next(index for index, task in enumerate(PLAY["tasks"]) if "ansible.builtin.replace" in task)
    assert PLAY["tasks"].index(idle) < first_mutation
