from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

from jinja2 import Environment, StrictUndefined
import pandas as pd
import pytest
import xarray as xr


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "roles/dashboard_services/templates"
PUBLISHER = TEMPLATES / "aurora-power-forecast-publish.py.j2"
CANDIDATE_LAUNCHER = TEMPLATES / "aurora-power-v12-candidate-launch.py.j2"
FORECAST_RUNNER = TEMPLATES / "aurora-power-forecast-runner.sh.j2"


def _template_context() -> dict[str, object]:
    return {
        "aurora_venv": "/opt/aurora-cloud-dashboard/venv",
        "aurora_app_dir": "/opt/aurora-cloud-dashboard",
        "aurora_service_user": "aurora",
        "aurora_service_group": "aurora",
        "aurora_power_forecast_orchestration_enabled": True,
        "aurora_zarr": {
            "power": "/data/aurora/products/power/power.zarr",
            "pdu": "/data/aurora/products/power/pdu.zarr",
            "asfs_logger": "/data/aurora/products/asfs_logger/asfs_logger.zarr",
            "power_soc_forecast": "/data/aurora/dev-products/power/forecast.zarr",
            "power_soc_forecast_archive": "/data/aurora/dev-products/power/archive.zarr",
            "power_soc_forecast_skill": "/data/aurora/dev-products/power/skill.zarr",
            "power_soc_hindcast": "/data/aurora/dev-products/power/hindcast.zarr",
            "power_soc_ensemble": "/data/aurora/dev-products/power/ensemble.zarr",
            "power_soc_ensemble_archive": "/data/aurora/dev-products/power/ensemble-archive.zarr",
            "power_soc_ensemble_skill": "/data/aurora/dev-products/power/ensemble-skill.zarr",
        },
        "aurora_power_planning_forecast_path": "/data/aurora/dev-products/power/planning.zarr",
        "aurora_power_planning_forecast_cache": "/data/aurora/dev-products/power/planning-cache",
        "aurora_power_planning_forecast_state": "/data/aurora/dev-products/power/planning-state.json",
        "aurora_power_planning_forecast_archive": "/data/aurora/dev-products/power/planning-archive.zarr",
        "aurora_power_planning_forecast_skill": "/data/aurora/dev-products/power/planning-skill.zarr",
        "aurora_power_planning_hindcast": "/data/aurora/dev-products/power/planning-hindcast.zarr",
        "aurora_ecmwf_provider": "legacy",
        "aurora_power_operating_model_state_path": "/data/aurora/dev-products/power/model.json",
        "aurora_power_forecast_root": "/data/aurora/dev-products/power",
        "aurora_power_operating_recommendations_path": "/data/aurora/dev-products/power/recommendations.json",
        "uas_source_destination": "/project/aurora/raw/menapia",
        "aurora_cl61_automation_history_path": "/data/aurora/dev-products/power/cl61-history.jsonl",
        "aurora_power_forecast_bundle_root": "/data/aurora/dev-products/power/forecast-bundle",
        "aurora_power_forecast_bundle_generations_root": "/data/aurora/dev-products/power/forecast-bundle/generations",
        "aurora_power_forecast_bundle_current_root": "/data/aurora/dev-products/power/forecast-bundle/current",
        "aurora_power_forecast_bundle_active_root": "/data/aurora/dev-products/power/forecast-bundle/active",
        "aurora_power_forecast_bundle_independent_root": "/data/aurora/dev-products/power/forecast-bundle/latest-independent",
        "aurora_power_forecast_bundle_status_path": "/data/aurora/dev-products/power/forecast_bundle_status.json",
        "aurora_power_forecast_bundle_history_path": "/data/aurora/dev-products/power/forecast_bundle_history.jsonl",
        "aurora_power_forecast_issue_root": "/data/aurora/dev-products/power/forecast-issues",
        "aurora_power_forecast_issue_staging_path": "/data/aurora/dev-products/power/forecast-issues/.incoming/forecast.zarr",
        "aurora_power_forecast_issue_current_path": "/data/aurora/dev-products/power/forecast-issues/current",
        "aurora_power_forecast_source_max_age_hours": 30,
        "aurora_power_forecast_source_max_cycle_delta_hours": 18,
        "aurora_power_forecast_generator_lock_path": "/data/aurora/dev-products/power/.deterministic-generator.lock",
        "aurora_power_forecast_publication_active": False,
        "aurora_power_v12_candidate_enabled": True,
        "aurora_paths": {
            "products_power_ecmwf_solar_forecast": "/data/aurora/dev-products/power/ecmwf_solar_forecast",
            "products_power_ecmwf_solar_ensemble": "/data/aurora/dev-products/power/ecmwf_solar_ensemble",
        },
        "aurora_power_v12_candidate_root": "/data/aurora/dev-products/power/candidates/v12",
        "aurora_power_v12_public_source_manifest_root": "/data/aurora/dev-products/power/public_model_inputs",
        "aurora_cl61_automation_shadow_enabled": True,
        "aurora_cl61_automation_environment": "development",
    }


def _render(template: Path) -> str:
    environment = Environment(undefined=StrictUndefined)
    environment.filters["to_json"] = json.dumps
    environment.filters["bool"] = bool
    environment.filters["quote"] = shlex.quote
    return environment.from_string(template.read_text()).render(**_template_context())


def _render_publisher() -> str:
    return _render(PUBLISHER)


def test_publisher_template_renders_as_python() -> None:
    compile(_render_publisher(), str(PUBLISHER), "exec")


def test_candidate_launcher_template_renders_as_python() -> None:
    compile(_render(CANDIDATE_LAUNCHER), str(CANDIDATE_LAUNCHER), "exec")


def test_candidate_launcher_persists_preflight_failures() -> None:
    launcher = CANDIDATE_LAUNCHER.read_text()

    assert 'RUN_STATUS = CANDIDATE_ROOT / "run_status.json"' in launcher
    assert 'EVALUATION_HISTORY = CANDIDATE_ROOT / "evaluation_history.jsonl"' in launcher
    assert 'stage="launcher_preflight"' in launcher
    assert 'reason_code="launcher_preflight_failed"' in launcher
    assert 'reason_code="launcher_execution_failed"' in launcher
    assert '"reason_code": "backend_failure_preserved"' in launcher
    assert 'record_launcher_status(\n        "running",' in launcher
    assert 'record_launcher_status(\n            "failed",' in launcher
    assert "atomic_json(RUN_STATUS, payload)" in launcher
    assert "if backend_recorded_detailed_failure():" in launcher
    backend_branch = launcher[
        launcher.index("if backend_recorded_detailed_failure():") :
        launcher.index("else:", launcher.index("if backend_recorded_detailed_failure():"))
    ]
    assert "atomic_json(RUN_STATUS" not in backend_branch
    assert "append_launcher_history(" in backend_branch
    assert "os.fsync(handle.fileno())" in launcher


def test_shared_forecast_runner_renders_as_bash(tmp_path: Path) -> None:
    helper = tmp_path / "aurora-power-forecast-runner"
    helper.write_text(_render(FORECAST_RUNNER))
    result = subprocess.run(
        ["bash", "-n", str(helper)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_artifact_digest_contract_has_fixed_vector(tmp_path: Path) -> None:
    namespace = {"__name__": "rendered_power_publisher"}
    exec(compile(_render_publisher(), str(PUBLISHER), "exec"), namespace)
    artifact = tmp_path / "example.zarr"
    (artifact / "nested").mkdir(parents=True)
    (artifact / "a.txt").write_bytes(b"alpha")
    (artifact / "nested/b.bin").write_bytes(b"\x00\xff")

    assert namespace["artifact_digest"](artifact) == (
        "b30b81d6b3aaa8da809cf52efe5a39de3b52a097e49a66d5aa0a44bc896b5eb2"
    )


def test_retention_never_removes_current_generation(tmp_path: Path) -> None:
    namespace = {"__name__": "rendered_power_publisher"}
    exec(compile(_render_publisher(), str(PUBLISHER), "exec"), namespace)
    generations = tmp_path / "generations"
    generations.mkdir()
    made = []
    for index in range(6):
        generation = generations / f"20260905T00000{index}Z-deadbeef"
        generation.mkdir()
        (generation / "generation.json").write_text('{"status":"complete"}\n')
        made.append(generation)
    current = tmp_path / "current"
    current.symlink_to(made[0].relative_to(tmp_path))
    namespace["GENERATIONS_ROOT"] = generations
    namespace["CURRENT_BUNDLE"] = current
    namespace["COMPLETE_GENERATIONS_TO_KEEP"] = 3

    namespace["retain_recent_generations"]()

    assert made[0].exists()
    assert len([item for item in generations.iterdir() if item.is_dir()]) == 4


def test_interrupted_issue_snapshot_is_quarantined_not_deleted(tmp_path: Path) -> None:
    namespace = {"__name__": "rendered_power_publisher"}
    exec(compile(_render_publisher(), str(PUBLISHER), "exec"), namespace)
    issue_root = tmp_path / "issues"
    incoming = issue_root / ".incoming/forecast.zarr"
    incoming.mkdir(parents=True)
    (incoming / "payload").write_bytes(b"recover-me")
    namespace["ISSUE_ROOT"] = issue_root

    namespace["quarantine_incoming"](incoming)

    assert not incoming.exists()
    recovered = list((issue_root / ".quarantine").glob("*/forecast.zarr/payload"))
    assert len(recovered) == 1
    assert recovered[0].read_bytes() == b"recover-me"


def test_interrupted_bundle_staging_is_quarantined_read_only(tmp_path: Path) -> None:
    namespace = {"__name__": "rendered_power_publisher"}
    exec(compile(_render_publisher(), str(PUBLISHER), "exec"), namespace)
    bundle_root = tmp_path / "forecast-bundle"
    generations = bundle_root / "generations"
    staging = generations / ".staging-20260906T011153Z-a3a8d307"
    staging.mkdir(parents=True)
    payload = staging / "partial-product.zarr/payload"
    payload.parent.mkdir()
    payload.write_bytes(b"recover-this-failed-generation")
    namespace["BUNDLE_ROOT"] = bundle_root
    namespace["GENERATIONS_ROOT"] = generations

    recovered = namespace["quarantine_interrupted_staging"]()

    assert not staging.exists()
    assert len(recovered) == 1
    assert recovered[0].parent == bundle_root / "quarantine"
    assert (recovered[0] / "partial-product.zarr/payload").read_bytes() == (
        b"recover-this-failed-generation"
    )
    marker = json.loads((recovered[0] / "interruption.json").read_text())
    assert marker["status"] == "failed_interrupted_staging"
    assert marker["originalName"] == staging.name
    assert all(
        not (item.stat().st_mode & 0o222)
        for item in (recovered[0], *recovered[0].rglob("*"))
    )


def test_bundle_publish_recovers_interrupted_staging_before_new_generation() -> None:
    publisher = PUBLISHER.read_text()

    call = "quarantine_interrupted_staging()"
    create = 'staging = GENERATIONS_ROOT / f".staging-{generation_id}"'
    publish_start = publisher.index("def publish_bundle(")
    publish_block = publisher[publish_start:]
    assert publish_block.index(call) < publish_block.index(create)


def test_issue_capture_verifies_marker_binds_manifest_and_removes_write_bits(
    tmp_path: Path,
) -> None:
    namespace = {"__name__": "rendered_power_publisher"}
    exec(compile(_render_publisher(), str(PUBLISHER), "exec"), namespace)
    issue_root = tmp_path / "issues"
    incoming = issue_root / ".incoming/forecast.zarr"
    incoming.parent.mkdir(parents=True)
    dataset = xr.Dataset(
        {"BatterySOCForecast": (("time",), [90.0, 91.0])},
        coords={"time": pd.date_range("2026-09-05T00:00:00", periods=2, freq="h")},
        attrs={
            "forecast_verification_eligible": "true",
            "forecast_refresh_kind": "ecmwf_cycle",
            "forecast_identity_id": "forecast-identity-one",
            "source_cycle_set_id": "ecmwf:cycle-one",
            "ecmwf_cycle_time": "2026-09-05T00:00:00+00:00",
            "generated_at_utc": "2026-09-05T00:05:00Z",
            "publication_signature": "publication-one",
        },
    )
    dataset.to_zarr(incoming, mode="w", consolidated=True)
    digest, file_count, byte_count = namespace["snapshot_content_stats"](incoming)
    (incoming / ".aurora-snapshot-content-v1.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "digestAlgorithm": "sha256-relative-path-nul-content-nul-v1",
                "contentDigest": f"sha256:{digest}",
                "fileCount": file_count,
                "byteCount": byte_count,
                "publicationSignature": "publication-one",
            }
        )
    )
    namespace["ISSUE_ROOT"] = issue_root
    namespace["CURRENT_ISSUE"] = issue_root / "current"
    namespace["STATUS_PATH"] = tmp_path / "status.json"
    namespace["HISTORY_PATH"] = tmp_path / "history.jsonl"

    namespace["capture_issue"](incoming)
    resolved = (issue_root / "current").resolve(strict=True)
    namespace["verify_issue_snapshot"](issue_root / "current")

    assert not incoming.exists()
    assert (resolved.parent / "issue_manifest.json").exists()
    assert json.loads((issue_root / "current_issue.json").read_text())["issueManifestDigest"].startswith(
        "sha256:"
    )
    assert all(not (item.stat().st_mode & 0o222) for item in (resolved.parent, *resolved.parent.rglob("*")))


def test_orchestration_is_development_only_and_production_default_is_off() -> None:
    defaults = (ROOT / "inventory/group_vars/aurora_cloud.yml").read_text()
    development = (ROOT / "inventory/host_vars/aurora-cloud-droplet.yml").read_text()
    production = (ROOT / "inventory/host_vars/aurora-cloud.yml").read_text()
    tasks = (ROOT / "roles/dashboard_services/tasks/main.yml").read_text()

    assert "aurora_power_forecast_orchestration_enabled: false" in defaults
    assert "aurora_power_forecast_orchestration_enabled: true" in development
    assert "aurora_power_v12_candidate_timer_enabled: false" in development
    assert "aurora_power_paired_evaluation_enabled: true" in development
    assert "aurora_power_paired_evaluation_enabled: false" in defaults
    assert "aurora_power_forecast_orchestration_enabled: true" not in production
    assert "Assert issue-driven Power orchestration is development-only" in tasks
    assert "aurora_failover_role != 'primary'" in tasks


def test_success_chain_orders_inputs_and_keeps_reanchors_non_independent() -> None:
    forecast = (TEMPLATES / "aurora-power-soc-forecast.service.j2").read_text()
    ensemble = (TEMPLATES / "aurora-power-soc-ensemble.service.j2").read_text()
    planning = (TEMPLATES / "aurora-power-soc-planning-forecast.service.j2").read_text()
    learn = (TEMPLATES / "aurora-power-soc-forecast-learn.service.j2").read_text()
    candidate = (TEMPLATES / "aurora-power-v12-candidate.service.j2").read_text()
    runner = FORECAST_RUNNER.read_text()
    launcher = CANDIDATE_LAUNCHER.read_text()

    assert "OnSuccess=aurora-power-soc-ensemble.service" not in forecast
    assert "OnSuccess=aurora-power-v12-candidate.service" in forecast
    assert "aurora-power-forecast-runner full-cycle" in forecast
    assert "--issue-snapshot-zarr" in runner
    assert "--quarantine-incoming" in runner
    assert "--print-issue-anchor" in runner
    assert '--power-cutoff-time "${issue_anchor}"' in runner
    assert "OnSuccess=aurora-power-soc-planning-forecast.service" not in ensemble
    assert "OnSuccess=aurora-power-forecast-publish.service" not in planning
    assert runner.index("--capture-issue") < runner.index("generate_power_soc_ensemble.py")
    assert runner.index("generate_power_soc_ensemble.py") < runner.index(
        "--horizon-hours 240"
    )
    assert runner.index("--horizon-hours 240") < runner.index('"${publisher}" --publish')
    assert "ExecCondition=/usr/local/bin/aurora-power-forecast-runner" in learn
    assert "OnSuccess=aurora-power-forecast-reanchor-publish.service" not in learn
    assert "--baseline-issue-zarr" in launcher
    assert "--baseline-ensemble-zarr" in launcher
    assert "--baseline-forecast-zarr" not in candidate
    assert "aurora_power_forecast_bundle_independent_root" in launcher
    assert "aurora_power_forecast_bundle_independent_root" in candidate


def test_development_orchestrator_has_a_host_protecting_memory_ceiling() -> None:
    template = TEMPLATES / "aurora-power-soc-forecast.service.j2"
    environment = Environment(undefined=StrictUndefined)
    environment.filters["bool"] = bool
    context = _template_context()
    rendered_development = environment.from_string(template.read_text()).render(**context)
    rendered_production = environment.from_string(template.read_text()).render(
        **{**context, "aurora_power_forecast_orchestration_enabled": False}
    )

    assert "MemoryHigh=3G" in rendered_development
    assert "MemoryMax=3500M" in rendered_development
    assert "OOMPolicy=stop" in rendered_development
    assert "MemoryMax=3500M" not in rendered_production


def test_deterministic_writers_share_lock_and_contention_skips_publication() -> None:
    runner = FORECAST_RUNNER.read_text()
    learn = (TEMPLATES / "aurora-power-soc-forecast-learn.service.j2").read_text()
    ensemble = (TEMPLATES / "aurora-power-soc-ensemble.service.j2").read_text()
    planning = (TEMPLATES / "aurora-power-soc-planning-forecast.service.j2").read_text()
    publisher = (TEMPLATES / "aurora-power-forecast-publish.service.j2").read_text()

    assert "aurora_power_forecast_generator_lock_path" in runner
    assert runner.count("/usr/bin/flock --exclusive") == 3
    assert "--nonblock --conflict-exit-code 75" in runner
    assert "return 1" in runner
    assert "cached Power re-anchor deferred" in runner
    assert runner.index("--refresh-from-cache") < runner.index("--allow-cached-reanchor")
    assert "ExecCondition=" in learn
    assert "ExecStart=/usr/bin/true" in learn
    assert "'45min' if aurora_power_forecast_orchestration_enabled" in learn
    assert "aurora-power-forecast-runner immutable-ensemble" in ensemble
    assert "/usr/bin/flock --exclusive --wait 1500" in planning
    assert "aurora_power_forecast_generator_lock_path" in planning
    assert "/usr/bin/flock --exclusive --wait 1500" in publisher
    assert "aurora_power_forecast_generator_lock_path" in publisher
    cached_publish = (
        '"${python}" "${publisher}" \\\n'
        "    --publish \\\n"
        "    --forecast-source"
    )
    assert cached_publish in runner
    cached_section = runner[
        runner.index("run_cached_reanchor_condition()") : runner.index('case "${action}"')
    ]
    assert "/usr/bin/flock --exclusive --wait 1500" not in cached_section
    assert "--refresh-from-cache" in cached_section
    assert "--no-archive-forecast" not in cached_section
    assert "--print-forecast-anchor" in cached_section
    assert "generate_power_soc_ensemble.py" in cached_section
    assert '--deterministic-zarr {{ aurora_zarr.power_soc_forecast | quote }}' in cached_section
    assert '--power-cutoff-time "${cached_anchor}"' in cached_section
    assert cached_section.index("generate_power_soc_ensemble.py") < cached_section.index(
        "--allow-cached-reanchor"
    )
    full_section = runner[
        runner.index("run_full_cycle()") : runner.index("run_immutable_ensemble()")
    ]
    assert full_section.count("/usr/bin/flock --exclusive") == 1
    assert "generate_power_soc_ensemble.py" in full_section
    assert '"${publisher}" --publish' in full_section


def test_manual_development_ensemble_is_immutable_issue_cutoff_bound() -> None:
    template = TEMPLATES / "aurora-power-soc-ensemble.service.j2"
    environment = Environment(undefined=StrictUndefined)
    environment.filters["bool"] = bool
    context = _template_context()
    rendered_development = environment.from_string(template.read_text()).render(**context)
    rendered_production = environment.from_string(template.read_text()).render(
        **{**context, "aurora_power_forecast_orchestration_enabled": False}
    )
    runner = FORECAST_RUNNER.read_text()
    manual_section = runner[
        runner.index("run_immutable_ensemble()") : runner.index(
            "run_cached_reanchor_condition()"
        )
    ]

    assert "ExecStart=/usr/local/bin/aurora-power-forecast-runner immutable-ensemble" in (
        rendered_development
    )
    assert "generate_power_soc_ensemble.py" not in rendered_development
    assert "immutable-ensemble" not in rendered_production
    assert "generate_power_soc_ensemble.py" in rendered_production
    assert "/usr/bin/flock --exclusive --wait 1500 9" in manual_section
    assert "--print-issue-anchor" in manual_section
    assert "generate_power_soc_ensemble.py" in manual_section
    assert '--power-cutoff-time "${issue_anchor}"' in manual_section
    assert manual_section.index("/usr/bin/flock --exclusive --wait 1500 9") < (
        manual_section.index("--print-issue-anchor")
    )
    assert manual_section.index("--print-issue-anchor") < manual_section.index(
        "generate_power_soc_ensemble.py"
    )


def test_publisher_fails_closed_when_deterministic_and_ensemble_anchors_differ() -> None:
    namespace = {"__name__": "rendered_power_publisher"}
    exec(compile(_render_publisher(), str(PUBLISHER), "exec"), namespace)
    deterministic = {"socAnchorUTC": "2026-09-05T00:00:00Z"}
    matching = {"socAnchorUTC": "2026-09-05T00:00:00+00:00"}
    mismatched = {"socAnchorUTC": "2026-09-05T00:10:00Z"}

    namespace["require_matching_soc_anchors"](deterministic, matching)
    with pytest.raises(RuntimeError, match="SOC anchors differ"):
        namespace["require_matching_soc_anchors"](deterministic, mismatched)


def test_publisher_requires_matching_ensemble_evidence_markers() -> None:
    publisher = PUBLISHER.read_text()

    assert 'expected_ensemble_kind = "cached_reanchor" if allow_cached_reanchor else "ecmwf_cycle"' in publisher
    assert "expected_ensemble_eligible = not allow_cached_reanchor" in publisher
    assert 'ensemble_attrs.get("forecast_verification_eligible", "").lower() == "true"' in publisher
    assert 'ensemble_attrs.get("independent_cycle", "").lower() == "true"' in publisher
    assert "ensemble independence/evidence markers do not match" in publisher
    assert "deterministic-plus-ens-site-forcing-v1" in publisher
    assert "power-ensemble-source-set-v1-[0-9a-f]{20}" in publisher
    assert 'ensemble_attrs.get("ensemble_site_forcing_sha256", "")' in publisher
    assert 'ensemble_attrs.get("deterministic_source_cycle_set_id")' in publisher
    assert 'ensemble_attrs.get("deterministic_source_manifest_digest")' in publisher
    assert 'ensemble_attrs.get("deterministic_forecast_identity_id")' in publisher
    assert "ensemble composite source identity differs from its inputs" in publisher


def test_bundle_switch_is_atomic_and_current_is_never_pruned() -> None:
    publisher = PUBLISHER.read_text()
    publish_unit = (TEMPLATES / "aurora-power-forecast-publish.service.j2").read_text()

    assert "os.replace(temporary, link)" in publisher
    assert "os.replace(staging, final)" in publisher
    assert '"status": "complete"' in publisher
    assert '"products": products' in publisher
    assert "relative.encode(\"utf-8\") + b\"\\0\"" in publisher
    assert "digest.update(b\"\\0\")" in publisher
    assert "resolved.parent != root or resolved == current" in publisher
    assert "COMPLETE_GENERATIONS_TO_KEEP = 12" in publisher
    assert "OnSuccess=aurora-power-v12-candidate.service" in publish_unit


def test_bundle_snapshots_and_hashes_every_forecast_source_before_use() -> None:
    publisher = PUBLISHER.read_text()

    planning_copy = "shutil.copytree(PLANNING_ZARR.resolve(strict=True), planning_snapshot)"
    ensemble_copy = "shutil.copytree(ENSEMBLE_ZARR.resolve(strict=True), ensemble_snapshot)"
    validation = "provenance, components = validate_inputs("
    assert publisher.index(planning_copy) < publisher.index(validation)
    assert publisher.index(ensemble_copy) < publisher.index(validation)
    assert '"--forecast-zarr", str(planning_snapshot)' in publisher
    assert '"--ensemble-zarr", str(ensemble_snapshot)' in publisher
    assert '"--recommendation-archive", str(staged_recommendation_history)' in publisher
    assert 'prior_bundle_product("operatingModelState", MODEL_STATE)' in publisher
    assert '"operatingRecommendationHistory", RECOMMENDATION_HISTORY' in publisher
    assert "shutil.copy2(\n                    recommendation_seed,\n                    staged_recommendation_history," in publisher
    assert '"power_operating_recommendations.json"' in publisher
    for logical_name in (
        '"deterministicForecast"',
        '"planningForecast"',
        '"ensembleForecast"',
        '"displayEnergy"',
        '"operatingRecommendationHistory"',
        '"cl61AutomationIntent"',
        '"cl61AutomationStatus"',
    ):
        assert logical_name in publisher
    assert '"sourceManifestProducts": source_manifest_products' in publisher
    assert '"sourceManifestDigest": f"sha256:{source_manifest_digest}"' in publisher
    assert '"upstreamSourceManifestDigest": components[label][' in publisher
    assert 'serializable["planning"]["path"] = "planning_forecast.zarr"' in publisher
    assert 'serializable["ensemble"]["path"] = "ensemble_forecast.zarr"' in publisher
    assert "make_tree_read_only(staging)" in publisher
    assert '"generated_at_utc",' in publisher
    assert 'payload.get("content_digest")' in publisher


def test_unpublished_bundle_never_mutates_legacy_recommendation_history() -> None:
    publisher = PUBLISHER.read_text()

    assert '"--recommendation-archive", str(RECOMMENDATION_HISTORY)' not in publisher
    scenario_start = publisher.index("scenario_command = [")
    scenario_end = publisher.index("run(scenario_command)")
    scenario_block = publisher[scenario_start:scenario_end]
    assert "staged_recommendation_history" in scenario_block


def test_adaptive_state_seeds_are_verified_from_the_previous_complete_bundle() -> None:
    publisher = PUBLISHER.read_text()

    assert "def prior_bundle_product(" in publisher
    assert 'manifest.get("status") != "complete"' in publisher
    assert "artifact_digest(target) != expected" in publisher
    assert 'prior_bundle_product("operatingModelState", MODEL_STATE)' in publisher
    assert 'prior_bundle_product(\n                "operatingRecommendationHistory"' in publisher


def test_source_validation_includes_cycle_age_and_all_90_hour_horizons() -> None:
    publisher = PUBLISHER.read_text()
    assert "has no explicit source-cycle timestamp" in publisher
    assert "source-cycle age" in publisher
    assert 'for label in ("deterministic", "planning", "ensemble")' in publisher
    assert "at least 90 h is required" in publisher


def test_candidate_is_bound_to_one_complete_independent_bundle() -> None:
    launcher = CANDIDATE_LAUNCHER.read_text()
    service = (TEMPLATES / "aurora-power-v12-candidate.service.j2").read_text()

    assert 'manifest.get("independentCycle") is not True' in launcher
    assert 'manifest.get("forecastRefreshKind") != "ecmwf_cycle"' in launcher
    assert '"deterministic": "deterministicForecast"' in launcher
    assert '"ensemble": "ensembleForecast"' in launcher
    assert "source-manifest digest mismatch" in launcher
    assert "combined source-cycle identity mismatch" in launcher
    assert '"--baseline-issue-zarr", str(resolved["deterministic"])' in launcher
    assert '"--baseline-ensemble-zarr", str(resolved["ensemble"])' in launcher
    assert '"--baseline-forcing-file"' not in launcher
    assert "ecmwf_input_file" not in launcher
    assert '"upstreamSourceManifestDigest"' in launcher
    assert "deterministic-plus-ens-site-forcing-v1" in launcher
    assert "candidate ensemble composite source identity is invalid" in launcher
    assert "def print_issue_anchor(" in PUBLISHER.read_text()
    assert "aurora_power_forecast_bundle_independent_root" in service
    assert "{{ aurora_zarr.power_soc_ensemble }}" not in service


def test_issue_snapshot_is_checksum_bound_and_made_read_only() -> None:
    publisher = PUBLISHER.read_text()
    assert 'SNAPSHOT_CONTENT_MARKER = ".aurora-snapshot-content-v1.json"' in publisher
    assert "snapshot_content_stats" in publisher
    assert '"snapshotMarkerDigest": f"sha256:{marker_digest}"' in publisher
    assert '"issueManifestDigest": f"sha256:{issue_manifest_digest}"' in publisher
    assert "make_tree_read_only(destination_dir)" in publisher
    assert "completed immutable issue remains writable" in publisher


def test_failure_status_preserves_first_reason_and_has_updated_at() -> None:
    publisher = PUBLISHER.read_text()
    assert '"updatedAt": now' in publisher
    assert '"requestedAt": payload.get("requestedAt") or now' in publisher
    assert 'previous.get("status") == "failed"' in publisher
    assert 'and previous.get("attemptID")' in publisher
    assert 'staleReason=previous["staleReason"]' in publisher
    assert "firstFailureAt" in publisher
    assert "def begin_attempt(" in publisher
    assert '"--begin-attempt"' in publisher
    assert "begin_attempt ecmwf_cycle" in FORECAST_RUNNER.read_text()
    assert "begin_attempt cached_reanchor" in FORECAST_RUNNER.read_text()


def test_second_failure_overlay_does_not_erase_first_detail(tmp_path: Path) -> None:
    namespace = {"__name__": "rendered_power_publisher"}
    exec(compile(_render_publisher(), str(PUBLISHER), "exec"), namespace)
    namespace["STATUS_PATH"] = tmp_path / "status.json"
    namespace["HISTORY_PATH"] = tmp_path / "history.jsonl"

    namespace["begin_attempt"]("ecmwf_cycle")
    namespace["mark_failure"]("bundle_publication", "specific digest mismatch")
    namespace["mark_failure"]("aurora-power-forecast-publish.service")
    status = json.loads((tmp_path / "status.json").read_text())

    assert status["staleReason"] == "bundle_publication failed: specific digest mismatch"
    assert status["failedStage"] == "bundle_publication"
    assert status["lastFailureStage"] == "aurora-power-forecast-publish.service"
    assert status["failureCount"] == 2
    assert status["updatedAt"]


def test_new_attempt_does_not_inherit_an_older_failure(tmp_path: Path) -> None:
    namespace = {"__name__": "rendered_power_publisher"}
    exec(compile(_render_publisher(), str(PUBLISHER), "exec"), namespace)
    namespace["STATUS_PATH"] = tmp_path / "status.json"
    namespace["HISTORY_PATH"] = tmp_path / "history.jsonl"

    namespace["begin_attempt"]("ecmwf_cycle")
    namespace["mark_failure"]("ensemble", "old failure")
    first = json.loads((tmp_path / "status.json").read_text())
    namespace["begin_attempt"]("cached_reanchor")
    namespace["mark_failure"]("bundle_publication", "new failure")
    second = json.loads((tmp_path / "status.json").read_text())

    assert first["attemptID"] != second["attemptID"]
    assert second["staleReason"] == "bundle_publication failed: new failure"
    assert second["failedStage"] == "bundle_publication"
    assert second["failureCount"] == 1


def test_new_attempt_clears_stale_pending_issue_identity(tmp_path: Path) -> None:
    namespace = {"__name__": "rendered_power_publisher"}
    exec(compile(_render_publisher(), str(PUBLISHER), "exec"), namespace)
    namespace["STATUS_PATH"] = tmp_path / "status.json"
    namespace["HISTORY_PATH"] = tmp_path / "history.jsonl"
    (tmp_path / "status.json").write_text(
        json.dumps(
            {
                "status": "current",
                "pendingForecastIdentityID": "stale-independent-identity",
                "pendingSourceCycleSetID": "stale-independent-cycle",
            }
        ),
        encoding="utf-8",
    )

    namespace["begin_attempt"]("cached_reanchor")
    status = json.loads((tmp_path / "status.json").read_text())

    assert status["status"] == "assembling"
    assert status["pendingForecastIdentityID"] is None
    assert status["pendingSourceCycleSetID"] is None


def test_completed_publication_clears_pending_issue_identity() -> None:
    publisher = PUBLISHER.read_text()
    completed_status = publisher[publisher.index('publish_status(\n                "current",') :]

    assert "pendingForecastIdentityID=None" in completed_status
    assert "pendingSourceCycleSetID=None" in completed_status


def test_independent_mutating_timers_are_disabled_when_orchestrated() -> None:
    tasks = (ROOT / "roles/dashboard_services/tasks/main.yml").read_text()
    display_service = (TEMPLATES / "aurora-dev-power-display-refresh.service.j2").read_text()
    scenario_service = (TEMPLATES / "aurora-power-operating-scenarios.service.j2").read_text()
    assert "Manage development Power display refresh timer" in tasks
    assert "not aurora_power_forecast_orchestration_enabled" in tasks
    assert "item.key == 'power_soc_ensemble'" in tasks
    assert "Manage long-range planning forecast timer" in tasks
    assert "Manage operating-state scenario timer" in tasks
    guard = "ConditionPathExists=!{{ aurora_power_forecast_bundle_current_root }}/generation.json"
    assert guard in display_service
    assert guard in scenario_service


def test_development_activates_only_after_explicit_bundle_validation() -> None:
    defaults = (ROOT / "inventory/group_vars/aurora_cloud.yml").read_text()
    development = (ROOT / "inventory/host_vars/aurora-cloud-droplet.yml").read_text()
    tasks = (ROOT / "roles/dashboard_services/tasks/main.yml").read_text()
    environment = (TEMPLATES / "aurora-dashboard.env.j2").read_text()

    assert "aurora_power_forecast_publication_active: false" in defaults
    assert "aurora_power_forecast_publication_active: true" in development
    assert "checksum-verified, complete v10" in development
    assert "CL61 actuation remains" in development
    assert "Bootstrap development Power bundle pointer" not in tasks
    assert "Refuse public Power bundle activation before a real generation exists" in tasks
    assert "Validate the public Power bundle activation marker" in tasks
    assert "Verify every artifact and identity in the candidate activation bundle" in tasks
    assert "--verify-bundle" in tasks
    assert "Activate the first validated development Power generation" in tasks
    assert tasks.index("--verify-bundle") < tasks.index(
        "Activate the first validated development Power generation"
    )
    assert "/usr/bin/readlink" in tasks
    resolve_task = tasks[
        tasks.index("Resolve the candidate development Power generation for activation") :
        tasks.index("Inspect the resolved candidate development Power bundle manifest")
    ]
    assert "check_mode: false" in resolve_task
    assert "aurora_power_forecast_activation_generation.stdout is match" in tasks
    assert 'src: current' not in tasks
    assert 'src: "{{ aurora_power_forecast_activation_generation.stdout }}"' in tasks
    verify_task = tasks[
        tasks.index("Verify every artifact and identity in the candidate activation bundle") :
        tasks.index("Activate the first validated development Power generation")
    ]
    assert "check_mode: false" in verify_task
    assert "AURORA_POWER_FORECAST_BUNDLE_READY_PATH=" in environment
    assert "AURORA_POWER_FORECAST_PUBLICATION_ACTIVE=" in environment
    assert "aurora_power_forecast_bundle_active_root" in environment


def test_activation_bundle_verifier_rejects_a_corrupt_product(tmp_path: Path) -> None:
    namespace = {"__name__": "rendered_power_publisher"}
    exec(compile(_render_publisher(), str(PUBLISHER), "exec"), namespace)
    generations = tmp_path / "generations"
    generation = generations / "20260905T000000Z-fixture"
    generation.mkdir(parents=True)
    namespace["GENERATIONS_ROOT"] = generations

    products: dict[str, dict[str, object]] = {}
    source_names = {
        "deterministic": "deterministicForecast",
        "planning": "planningForecast",
        "ensemble": "ensembleForecast",
    }
    component_values = {
        "deterministic": ("deterministic-id", "deterministic-cycle"),
        "planning": ("planning-id", "planning-cycle"),
        "ensemble": ("ensemble-id", "ensemble-cycle"),
    }
    for logical_name, relative_path in namespace["REQUIRED_BUNDLE_PRODUCTS"].items():
        target = generation / relative_path
        payload_target = target
        if relative_path.endswith(".zarr"):
            target.mkdir()
            payload_target = target / "payload"
        if logical_name == "cl61AutomationIntent":
            payload_target.write_text(
                json.dumps(
                    {
                        "authority": "diagnostic",
                        "safety": {"control_eligible": False},
                    }
                )
            )
        elif logical_name == "cl61AutomationStatus":
            payload_target.write_text(
                json.dumps(
                    {
                        "control_authority": "observe_only",
                        "mode": "observe_only",
                        "capability": False,
                    }
                )
            )
        else:
            payload_target.write_bytes(f"fixture:{logical_name}".encode())
        record: dict[str, object] = {
            "relativePath": relative_path,
            "sha256": namespace["artifact_digest"](target),
        }
        if logical_name in source_names.values():
            label = next(key for key, value in source_names.items() if value == logical_name)
            record["forecastIdentityID"] = component_values[label][0]
        products[logical_name] = record

    components: dict[str, dict[str, object]] = {}
    source_products: dict[str, dict[str, object]] = {}
    for label, product_name in source_names.items():
        identity, cycle = component_values[label]
        component = {
            "forecastIdentityID": identity,
            "sourceCycleSetID": cycle,
            "upstreamSourceManifestDigest": f"sha256:upstream-{label}",
            "socAnchorUTC": "2026-09-05T00:00:00Z",
        }
        components[label] = component
        source_products[label] = {
            "product": product_name,
            "relativePath": products[product_name]["relativePath"],
            "sha256": products[product_name]["sha256"],
            "productIdentityID": identity,
            **component,
        }
    cycle_map = {
        label: str(component["sourceCycleSetID"])
        for label, component in components.items()
    }
    manifest = {
        "schemaVersion": 1,
        "status": "complete",
        "controlAuthority": "advisory_only",
        "cl61ActuationEnabled": False,
        "forecastRefreshKind": "ecmwf_cycle",
        "independentCycle": True,
        "forecastIdentityID": components["deterministic"]["forecastIdentityID"],
        "sourceCycleSetID": "power-cycle-set-v1-"
        + hashlib.sha256(json.dumps(cycle_map, sort_keys=True).encode()).hexdigest()[:20],
        "sourceManifestDigest": "sha256:"
        + namespace["canonical_json_digest"](source_products),
        "components": components,
        "sourceManifestProducts": source_products,
        "products": products,
    }
    (generation / "generation.json").write_text(json.dumps(manifest))
    for item in generation.rglob("*"):
        item.chmod(0o555 if item.is_dir() else 0o444)
    generation.chmod(0o555)

    try:
        namespace["verify_bundle"](generation)
        corrupt = (
            generation
            / namespace["REQUIRED_BUNDLE_PRODUCTS"]["displayEnergy"]
            / "payload"
        )
        corrupt.chmod(0o644)
        corrupt.write_bytes(b"corrupt")
        corrupt.chmod(0o444)
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            namespace["verify_bundle"](generation)
    finally:
        generation.chmod(0o755)
        for item in generation.rglob("*"):
            item.chmod(0o755 if item.is_dir() else 0o644)


def test_activation_bundle_verifier_failure_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    namespace = {"__name__": "rendered_power_publisher"}
    exec(compile(_render_publisher(), str(PUBLISHER), "exec"), namespace)
    generations = tmp_path / "generations"
    generation = generations / "20260905T000000Z-fixture"
    generation.mkdir(parents=True)
    active = tmp_path / "active"
    active.symlink_to(generation.relative_to(tmp_path))
    status = tmp_path / "status.json"
    history = tmp_path / "history.jsonl"
    original_status = '{"status":"current"}\n'
    original_history = '{"status":"current"}\n'
    status.write_text(original_status)
    history.write_text(original_history)
    namespace["GENERATIONS_ROOT"] = generations
    namespace["STATUS_PATH"] = status
    namespace["HISTORY_PATH"] = history
    monkeypatch.setattr(
        sys, "argv", ["aurora-power-forecast-publish", "--verify-bundle", str(active)]
    )

    assert namespace["main"]() == 1
    assert status.read_text() == original_status
    assert history.read_text() == original_history
    assert (
        "bundle_activation_verification failed: "
        "bundle verification requires a direct generation path"
    ) in capsys.readouterr().err


def test_activated_consumers_use_only_the_validated_active_pointer() -> None:
    defaults = (ROOT / "inventory/group_vars/aurora_cloud.yml").read_text()
    environment = (TEMPLATES / "aurora-dashboard.env.j2").read_text()

    activated_products = defaults[defaults.index("aurora_power_operating_state_path:") :]
    for filename in (
        "power_operating_state.zarr",
        "power_operating_scenarios.zarr",
        "power_operating_model_state.json",
        "cl61_automation_intent.json",
        "cl61_automation_status.json",
        "power_display_summary.zarr",
        "power_display_energy.zarr",
        "power_current_display.zarr",
        "power_forecast_display.zarr",
        "power_display_manifest.json",
    ):
        block_start = activated_products.index(filename) - 120
        block_end = activated_products.index(filename) + len(filename)
        assert "aurora_power_forecast_bundle_active_root" in activated_products[
            max(block_start, 0) : block_end
        ]
    assert (
        "AURORA_POWER_FORECAST_BUNDLE_MANIFEST_PATH="
        "{{ aurora_power_forecast_bundle_active_root }}/generation.json"
    ) in environment
    assert (
        "AURORA_POWER_OPERATIONAL_FORECAST_SNAPSHOT_PATH="
        "{{ aurora_power_forecast_bundle_active_root }}/operational_forecast.zarr"
    ) in environment


def test_paired_evaluation_is_bounded_and_not_an_operational_dependency() -> None:
    candidate = (TEMPLATES / "aurora-power-v12-candidate.service.j2").read_text()
    evaluator = (TEMPLATES / "aurora-power-prod-dev-evaluation.service.j2").read_text()
    publisher = (TEMPLATES / "aurora-power-forecast-publish.service.j2").read_text()
    tasks = (ROOT / "roles/dashboard_services/tasks/main.yml").read_text()

    assert "OnSuccess=aurora-power-prod-dev-evaluation.service" in candidate
    assert "OnFailure=aurora-power-prod-dev-evaluation.service" in candidate
    assert "generate_power_prod_dev_evaluation.py" in evaluator
    assert "--prod-archive-zarr {{ aurora_product_root }}" in evaluator
    assert "--dev-archive-zarr {{ aurora_zarr.power_soc_forecast_archive }}" in evaluator
    assert "MemoryMax=1.5G" in evaluator
    assert "ConditionPathExists=" not in evaluator
    assert "aurora-power-prod-dev-evaluation" not in publisher
    assert "Remove a disabled development paired Power evaluator unit" in tasks


def test_cl61_remains_observe_only() -> None:
    publisher = PUBLISHER.read_text()
    assert '"controlAuthority": "advisory_only"' in publisher
    assert '"cl61ActuationEnabled": False' in publisher
    assert "--enable-automation-shadow" in publisher
    assert "validate_cl61_observe_only" in publisher
    assert 'safety.get("control_eligible") is not False' in publisher
    assert 'status.get("capability") is not False' in publisher
    assert "pdu-control" not in publisher.lower()
