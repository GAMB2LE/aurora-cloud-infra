from pathlib import Path
import shlex
import subprocess

from jinja2 import Environment, StrictUndefined


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "roles/release_snapshot/templates/aurora-release-snapshot.sh.j2"
ROLLBACK_TEMPLATE = ROOT / "roles/release_snapshot/templates/aurora-development-rollback.sh.j2"
POWER_BASELINE_TEMPLATE = (
    ROOT / "roles/release_snapshot/templates/aurora-power-baseline-snapshot.sh.j2"
)


def _render_template() -> str:
    content = TEMPLATE.read_text()
    replacements = {
        "{{ aurora_release_snapshot_root | quote }}": "'/var/lib/aurora-release-snapshots'",
        "{{ aurora_app_dir | quote }}": "'/opt/aurora-cloud-dashboard'",
        "{{ aurora_service_user | quote }}": "'aurora'",
        "{{ aurora_service_group | quote }}": "'aurora'",
        "{{ aurora_uv_bin | quote }}": "'/usr/local/bin/uv'",
    }
    for source, replacement in replacements.items():
        content = content.replace(source, replacement)
    return content


def _render_power_baseline() -> str:
    context = {
        "aurora_site_env": "development",
        "aurora_failover_role": "standby",
        "aurora_power_forecast_publication_active": False,
        "aurora_power_baseline_snapshot_root": "/var/lib/aurora-power-baseline-snapshots",
        "aurora_power_forecast_root": "/data/aurora/dev-products/power",
        "aurora_app_dir": "/opt/aurora-cloud-dashboard",
        "aurora_service_user": "aurora",
        "aurora_service_group": "aurora",
        "aurora_power_forecast_generator_lock_path": "/data/aurora/dev-products/power/.deterministic-generator.lock",
        "aurora_zarr": {
            "power_soc_forecast": "/data/aurora/dev-products/power/power_soc_forecast.zarr",
            "power_soc_forecast_archive": "/data/aurora/dev-products/power/power_soc_forecast_archive.zarr",
            "power_soc_forecast_skill": "/data/aurora/dev-products/power/power_soc_forecast_skill.zarr",
            "power_soc_hindcast": "/data/aurora/dev-products/power/power_soc_hindcast.zarr",
            "power_soc_ensemble": "/data/aurora/dev-products/power/power_soc_ensemble_forecast.zarr",
            "power_soc_ensemble_archive": "/data/aurora/dev-products/power/power_soc_ensemble_archive.zarr",
            "power_soc_ensemble_skill": "/data/aurora/dev-products/power/power_soc_ensemble_skill.zarr",
        },
        "aurora_power_planning_forecast_path": "/data/aurora/dev-products/power/power_soc_planning_forecast.zarr",
        "aurora_power_planning_forecast_state": "/data/aurora/dev-products/power/power_soc_planning_state.json",
        "aurora_power_planning_forecast_archive": "/data/aurora/dev-products/power/power_soc_planning_archive.zarr",
        "aurora_power_planning_forecast_skill": "/data/aurora/dev-products/power/power_soc_planning_skill.zarr",
        "aurora_power_planning_hindcast": "/data/aurora/dev-products/power/power_soc_planning_hindcast.zarr",
        "aurora_power_operating_state_path": "/data/aurora/dev-products/power/power_operating_state.zarr",
        "aurora_power_operating_scenarios_path": "/data/aurora/dev-products/power/power_operating_scenarios.zarr",
        "aurora_power_operating_model_state_path": "/data/aurora/dev-products/power/power_operating_model_state.json",
        "aurora_power_operating_recommendations_path": "/data/aurora/dev-products/power/power_operating_recommendations.json",
        "aurora_power_display_summary_path": "/data/aurora/dev-products/power/power_display_summary.zarr",
        "aurora_power_display_energy_path": "/data/aurora/dev-products/power/power_display_energy.zarr",
        "aurora_power_current_display_path": "/data/aurora/dev-products/power/power_current_display.zarr",
        "aurora_power_forecast_display_path": "/data/aurora/dev-products/power/power_forecast_display.zarr",
        "aurora_power_display_manifest_path": "/data/aurora/dev-products/power/power_display_manifest.json",
        "aurora_cl61_automation_intent_path": "/data/aurora/dev-products/power/cl61_automation_intent.json",
        "aurora_cl61_automation_status_path": "/data/aurora/dev-products/power/cl61_automation_status.json",
        "aurora_cl61_automation_history_path": "/data/aurora/dev-products/power/cl61_automation_history.jsonl",
    }
    environment = Environment(undefined=StrictUndefined)
    environment.filters["quote"] = shlex.quote
    environment.filters["bool"] = bool
    return environment.from_string(POWER_BASELINE_TEMPLATE.read_text()).render(**context)


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
    assert "--ignore-failed-read" not in content
    assert "aurora-release-snapshot-verify" in content
    assert 'cd "${verification_root}"' in content
    assert "snapshot.sha256" in content
    assert "service-states.tsv" in content
    assert "index.patch" in content
    assert "git -C \"${app_dir}\" diff --cached --binary" in content
    assert "aurora-power-append.service" in content
    assert "aurora-pdu-append.service" in content
    assert "aurora-dev-live-pull@product-power.service" in content


def test_development_rollback_is_explicit_checksum_verified_and_data_safe(tmp_path: Path) -> None:
    content = ROLLBACK_TEMPLATE.read_text()
    replacements = {
        "{{ aurora_release_snapshot_root | quote }}": "'/var/lib/aurora-release-snapshots'",
        "{{ aurora_app_dir | quote }}": "'/opt/aurora-cloud-dashboard'",
        "{{ aurora_service_user | quote }}": "'aurora'",
        "{{ aurora_service_group | quote }}": "'aurora'",
        "{{ aurora_uv_bin | quote }}": "'/usr/local/bin/uv'",
    }
    for source, replacement in replacements.items():
        content = content.replace(source, replacement)
    helper = tmp_path / "aurora-development-rollback"
    helper.write_text(content)
    result = subprocess.run(
        ["bash", "-n", str(helper)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert "sha256sum --check --strict snapshot.sha256" in content
    assert 'sha256sum --check --strict "${snapshot_dir}/configuration.sha256"' in content
    assert "^[0-9a-f]{40}$" in content
    assert 'dashboard_revision}" != "${source_commit' in content
    assert '"${uv_bin}" pip sync' in content
    assert "diff --cached --quiet" in content
    assert content.count("require_clean_checkout") >= 3
    assert 'rollback cannot proceed while ${unit}' in content
    assert "aurora-power-prod-dev-evaluation.service" in content
    assert "aurora-power-append.timer" in content
    assert "aurora-pdu-append.timer" in content
    assert "aurora-dev-live-pull-product-power.timer" in content
    assert "aurora-power-forecast-bundle-failure@*.service" in content
    assert 'for reader in aurora-dashboard.service aurora-mobile-api.service' in content
    assert "aurora-cloud-droplet" not in content  # host scoping is in the playbook
    assert "/data/" not in content


def test_development_rollback_playbook_cannot_target_production() -> None:
    playbook = (ROOT / "playbooks/dev_dashboard_rollback.yml").read_text()
    assert "hosts: aurora-cloud-droplet" in playbook
    assert "aurora_site_env == 'development'" in playbook
    assert "aurora_failover_role != 'primary'" in playbook
    assert "aurora_rollback_snapshot_name is defined" in playbook
    assert "aurora_rollback_dashboard_revision is defined" in playbook


def test_power_baseline_snapshot_helper_is_dev_only_explicit_and_valid_bash(
    tmp_path: Path,
) -> None:
    content = _render_power_baseline()
    helper = tmp_path / "aurora-power-baseline-snapshot"
    helper.write_text(content)
    result = subprocess.run(
        ["bash", "-n", str(helper)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert "refusing Power baseline snapshot outside unpublished development" in content
    assert "refusing unexpected immutable snapshot root" in content
    assert "/data/aurora/dev-products/power" in content
    assert "/data/aurora/products" not in content
    assert "copy_artifact deterministicForecast" in content
    assert "copy_artifact deterministicState" in content
    assert "copy_artifact deterministicArchive" in content
    assert "copy_artifact deterministicSkill" in content
    assert "copy_artifact deterministicHindcast" in content
    assert "copy_artifact ensembleForecast" in content
    assert "copy_artifact ensembleArchive" in content
    assert "copy_artifact ensembleSkill" in content
    assert "copy_artifact planningForecast" in content
    assert "copy_artifact planningState" in content
    assert "copy_artifact planningArchive" in content
    assert "copy_artifact planningSkill" in content
    assert "copy_artifact planningHindcast" in content
    assert "copy_artifact operatingState" in content
    assert "copy_artifact operatingScenarios" in content
    assert "copy_artifact displayEnergy" in content
    assert "copy_optional_artifact cl61AutomationIntent" in content
    assert "copy_optional_artifact cl61AutomationStatus" in content
    assert "copy_optional_artifact cl61AutomationHistory" in content
    copy_lines = [
        line.lower()
        for line in content.splitlines()
        if line.startswith(("copy_artifact ", "copy_optional_artifact "))
    ]
    assert all("ecmwf_solar" not in line and ".grib" not in line for line in copy_lines)
    assert "copy_artifact forecastBundle" not in content
    assert "cp -a --reflink=auto" in content
    assert "find \"${resolved_source}\" ! -type f ! -type d" in content
    assert "sha256-relative-path-nul-content-nul-v1" in content
    assert 'relative.encode("utf-8") + b"\\0"' in content
    assert 'digest.update(b"\\0")' in content
    assert "sha256sum --check --strict manifest.sha256" in content
    assert "find \"${staging_dir}\" -type f -exec chmod 0400" in content
    assert "find \"${staging_dir}\" -type d -exec chmod 0500" in content
    assert "mv -- \"${staging_dir}\" \"${final_dir}\"" in content
    assert "incomplete snapshot preserved" in content
    assert "source_fingerprint_before=$(artifact_fingerprint" in content
    assert "source_fingerprint_after=$(artifact_fingerprint" in content
    assert "destination_fingerprint=$(artifact_fingerprint" in content
    assert 'source_fingerprint_before} != "${source_fingerprint_after}' in content
    assert '"sourceFingerprintVerified": True' in content
    assert "final_source_fingerprint=$(artifact_fingerprint" in content
    assert "baseline source changed before manifest finalization" in content
    assert 'generator_lock} != "${allowed_product_root}/.deterministic-generator.lock"' in content
    assert 'install -o "${service_user}" -g "${service_group}" -m 0600 /dev/null "${generator_lock}"' in content
    assert 'chown "${service_user}:${service_group}" -- "${generator_lock}"' in content
    assert content.index('chown "${service_user}:${service_group}" -- "${generator_lock}"') < content.index('exec 9>"${generator_lock}"')
    assert "status --porcelain=v1 --untracked-files=all" in content
    assert 'systemctl start --no-block "${unit}"' in content
    assert 'systemctl cat "${unit}" > "${definition_path}"' in content
    assert '("unitDefinitions", "metadata/unit-definitions")' in content
    assert '"captureCheckoutCommit": source_commit' in content
    assert "prohibited cache/global-grid entry" in content
    assert "aurora-power-prod-dev-evaluation.service" in content
    assert "aurora-power-forecast-bundle-failure@*.service" in content
    assert "AURORA_POWER_" in content
    assert "cp -a --reflink=auto -- /etc/aurora-dashboard.env" not in content


def test_power_baseline_snapshot_playbook_requires_explicit_unpublished_dev_target() -> None:
    playbook = (ROOT / "playbooks/power_v10_baseline_snapshot.yml").read_text()
    defaults = (ROOT / "inventory/group_vars/aurora_cloud.yml").read_text()
    development = (ROOT / "inventory/host_vars/aurora-cloud-droplet.yml").read_text()

    assert "hosts: aurora-cloud-droplet" in playbook
    assert "aurora_site_env == 'development'" in playbook
    assert "aurora_failover_role != 'primary'" in playbook
    assert "aurora_power_forecast_publication_active | bool == false" in playbook
    assert "aurora_power_baseline_snapshot_name is defined" in playbook
    assert "aurora_release_snapshot_capture_on_run: false" in playbook
    assert "aurora_power_baseline_snapshot_enabled: false" in defaults
    assert "aurora_power_baseline_snapshot_enabled: true" in development


def test_snapshot_precedes_dashboard_checkout_in_all_release_playbooks() -> None:
    for name in (
        "dashboard_release.yml",
        "dashboard_runtime_release.yml",
        "dashboard_security_release.yml",
        "mobile_api_release.yml",
    ):
        content = (ROOT / "playbooks" / name).read_text()
        assert content.index("- release_snapshot") < content.index("- dashboard_app")
