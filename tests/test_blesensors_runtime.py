import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
VARIABLES = yaml.safe_load((ROOT / "inventory/group_vars/aurora_cloud.yml").read_text())
TEMPLATE = ROOT / "roles/dashboard_services/templates/aurora-blesensors-append.service.j2"


class BLERuntimeTests(unittest.TestCase):
    def setUp(self):
        self.environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        self.environment.filters["bool"] = bool

    def test_default_paths_retain_the_shared_installation(self):
        shared = {"aurora_app_dir": "/existing/app", "aurora_venv": "/existing/runtime"}
        for selected, original in (("blesensors_app_dir", "aurora_app_dir"),
                                   ("blesensors_venv", "aurora_venv")):
            self.assertEqual(self.environment.from_string(VARIABLES[selected]).render(**shared), shared[original])

    def test_service_runs_selected_code_and_python_with_existing_data_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "reviewed-release"
            runtime = root / "reviewed-runtime"
            app.mkdir()
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin/python").symlink_to(sys.executable)
            (app / "append_new_blesensors_to_zarr.py").write_text(
                "import json,os,sys\nprint(json.dumps({'cwd':os.getcwd(),'args':sys.argv[1:]}))\n"
            )
            rendered = self.environment.from_string(TEMPLATE.read_text()).render(
                aurora_app_dir="/old/writer-without-appender", aurora_venv="/old/writer-runtime",
                blesensors_app_dir=str(app), blesensors_venv=str(runtime),
                aurora_service_user="aurora", aurora_service_group="aurora",
                aurora_state_root="/unchanged/state",
                aurora_paths={"raw_blesensors": "/unchanged/raw/blesensors"},
                aurora_zarr={"blesensors": "/unchanged/products/blesensors.zarr"},
            )
            values = dict(line.split("=", 1) for line in rendered.splitlines() if "=" in line)
            command = shlex.split(values["ExecStart"])
            self.assertEqual(command[:3], ["/usr/bin/flock", "--nonblock", "/unchanged/state/blesensors-append.lock"])
            self.assertEqual(command[3], str(runtime / "bin/python"))
            result = subprocess.run(command[3:], cwd=values["WorkingDirectory"], text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout), {
                "cwd": str(app.resolve()),
                "args": ["--root", "/unchanged/raw/blesensors", "--zarr", "/unchanged/products/blesensors.zarr"],
            })
            self.assertEqual(values["EnvironmentFile"], "/etc/aurora-dashboard.env")

    def test_preflight_checks_the_same_selected_paths_and_blocks_missing_runtime(self):
        tasks = yaml.safe_load((ROOT / "playbooks/blesensors.yml").read_text())[0]["pre_tasks"]
        appender = next(task for task in tasks if task.get("register") == "blesensors_appender")
        python = next(task for task in tasks if task.get("register") == "blesensors_python")
        selected = {"blesensors_app_dir": "/reviewed/code", "blesensors_venv": "/reviewed/runtime"}
        self.assertEqual(self.environment.from_string(appender["ansible.builtin.stat"]["path"]).render(**selected),
                         "/reviewed/code/append_new_blesensors_to_zarr.py")
        self.assertEqual(self.environment.from_string(python["ansible.builtin.stat"]["path"]).render(**selected),
                         "/reviewed/runtime/bin/python")
        self.assertTrue(python["ansible.builtin.stat"]["follow"])
        assertions = next(task["ansible.builtin.assert"]["that"] for task in tasks
                          if task["name"] == "Require appender code and runtime before enabling BLE processing")
        def allowed(code, executable, enabled=True, check=False):
            variables = dict(blesensors_appender={"stat": {"isreg": code}},
                             blesensors_python={"stat": {"executable": executable}},
                             aurora_processing_timers={"blesensors_append": enabled},
                             aurora_writer_timers_enabled=True, ansible_check_mode=check)
            return all(self.environment.compile_expression(item)(**variables) for item in assertions)
        self.assertTrue(allowed(True, True))
        self.assertFalse(allowed(False, True))
        self.assertFalse(allowed(True, False))
        self.assertTrue(allowed(False, False, enabled=False))
        self.assertTrue(allowed(False, False, check=True))

    def test_only_production_ble_template_uses_the_new_paths(self):
        users = {path.relative_to(ROOT).as_posix() for path in (ROOT / "roles").rglob("*.j2")
                 if "blesensors_app_dir" in path.read_text() or "blesensors_venv" in path.read_text()}
        self.assertEqual(users, {TEMPLATE.relative_to(ROOT).as_posix()})


if __name__ == "__main__":
    unittest.main()
