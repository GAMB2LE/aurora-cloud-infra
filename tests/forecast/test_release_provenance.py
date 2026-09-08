"""Exercise the real release metadata task against an isolated environment file."""
from copy import deepcopy
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
REVISION = "db6bc869cef05254569f8e3599883062f2fddce0"


@pytest.mark.parametrize("old_revision", ["old-release", None])
def test_release_metadata_preserves_other_environment_bytes(tmp_path, old_revision):
    env = tmp_path / "dashboard.env"
    prefix = b"# Commissioned settings\nPROTECTED_VALUE=do-not-display-this-sentinel\n"
    suffix = b"AURORA_CL61_ACTUATION_ENABLED=false\n"
    old_line = (f"AURORA_FORECAST_CODE_REVISION={old_revision}\n".encode()
                if old_revision is not None else b"")
    original = prefix + old_line + suffix
    env.write_bytes(original)
    env.chmod(0o600)
    before = env.stat()
    tasks = yaml.safe_load((ROOT / "playbooks/mobile_api_code_release.yml").read_text())[0]["tasks"]
    task = deepcopy(next(t for t in tasks if t["name"] == "Label future forecast products with the exact installed source"))
    task["ansible.builtin.lineinfile"]["path"] = str(env)
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "become": False,
             "vars": {"aurora_mobile_api_target_revision": REVISION}, "tasks": [task]}]
    playbook = tmp_path / "release.yml"
    playbook.write_text(yaml.safe_dump(play))
    executable = shutil.which("ansible-playbook")
    assert executable, "The locked infrastructure runtime must provide Ansible"
    args = [executable, "-i", "localhost,", str(playbook), "--diff"]
    command_env = {**os.environ, "ANSIBLE_NOCOLOR": "1"}
    for extra in (["--check"], [], []):
        result = subprocess.run(args + extra, capture_output=True, text=True, env=command_env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "do-not-display-this-sentinel" not in result.stdout + result.stderr
        if extra:
            assert env.read_bytes() == original
        else:
            current = env.read_bytes()
            replacement = f"AURORA_FORECAST_CODE_REVISION={REVISION}\n".encode()
            expected = prefix + replacement + suffix if old_revision is not None else original + replacement
            assert current == expected
            after = env.stat()
            assert (after.st_mode, after.st_uid, after.st_gid) == (before.st_mode, before.st_uid, before.st_gid)
