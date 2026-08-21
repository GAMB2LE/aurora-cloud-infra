from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "roles/release_snapshot/templates/aurora-release-snapshot.sh.j2"


def _render_template() -> str:
    content = TEMPLATE.read_text()
    replacements = {
        "{{ aurora_release_snapshot_root | quote }}": "'/var/lib/aurora-release-snapshots'",
        "{{ aurora_app_dir | quote }}": "'/opt/aurora-cloud-dashboard'",
        "{{ aurora_service_user | quote }}": "'aurora'",
    }
    for source, replacement in replacements.items():
        content = content.replace(source, replacement)
    return content


def test_release_snapshot_helper_renders_as_valid_bash(tmp_path: Path) -> None:
    helper = tmp_path / "aurora-release-snapshot"
    helper.write_text(_render_template())
    result = subprocess.run(
        ["bash", "-n", str(helper)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_release_snapshot_archives_configuration_without_exposing_it_in_git() -> None:
    content = _render_template()
    assert "configuration.tar.gz" in content
    assert "umask 077" in content
    assert "chmod 0600" in content
    assert "/etc/aurora-mobile-api.token" in content
    assert "tar -C /" in content


def test_snapshot_precedes_dashboard_checkout_in_all_release_playbooks() -> None:
    for name in (
        "dashboard_release.yml",
        "dashboard_runtime_release.yml",
        "dashboard_security_release.yml",
        "mobile_api_release.yml",
    ):
        content = (ROOT / "playbooks" / name).read_text()
        assert content.index("- release_snapshot") < content.index("- dashboard_app")
