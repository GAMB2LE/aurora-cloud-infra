from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RETIRED_UNIT = "aurora-les-operational-run"
RETIRED_CONFIG_TOKENS = (
    "aurora_leeds_operational_20260521_rolling.yaml",
    "aurora_les_campaign_config",
    "aurora_les_era5_lag_days",
    "les_operational_run",
)


def test_dashboard_role_does_not_install_retired_science_units() -> None:
    tasks = (ROOT / "roles/dashboard_services/tasks/main.yml").read_text()
    install_tasks = tasks.split(
        "- name: Install systemd service and timer units", maxsplit=1
    )[1]

    assert f"- {RETIRED_UNIT}.service" not in install_tasks
    assert f"- {RETIRED_UNIT}.timer" not in install_tasks
    assert "aurora-model-evaluation-daily" not in install_tasks
    assert not (
        ROOT
        / "roles/dashboard_services/templates"
        / f"{RETIRED_UNIT}.service.j2"
    ).exists()
    assert not (
        ROOT
        / "roles/dashboard_services/templates"
        / f"{RETIRED_UNIT}.timer.j2"
    ).exists()


def test_dashboard_role_removes_retired_science_units_from_hosts() -> None:
    tasks = (ROOT / "roles/dashboard_services/tasks/main.yml").read_text()

    assert "Stop and disable the retired Leeds ERA5 science units" in tasks
    assert "Remove the retired Leeds ERA5 science unit files" in tasks
    assert f"- {RETIRED_UNIT}.service" in tasks
    assert f"- {RETIRED_UNIT}.timer" in tasks


def test_active_inventory_has_no_retired_science_configuration() -> None:
    active_files = (
        ROOT / "inventory/group_vars/aurora_cloud.yml",
        ROOT / "inventory/host_vars/aurora-cloud-droplet.yml",
        ROOT / "roles/dashboard_services/templates/aurora-dashboard.env.j2",
    )

    for path in active_files:
        text = path.read_text()
        for token in RETIRED_CONFIG_TOKENS:
            assert token not in text, f"{token} remains active in {path}"
