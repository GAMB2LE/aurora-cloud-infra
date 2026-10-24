from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from test_power_forecast_orchestration import ROOT, FORECAST_RUNNER, _render, _render_publisher

HELPER = ROOT / "roles/dashboard_services/files/aurora-power-forecast-inputs.py"
spec = importlib.util.spec_from_file_location("forecast_inputs", HELPER)
inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inputs)


@pytest.fixture
def generation(tmp_path, monkeypatch):
    root = tmp_path / "continuity"
    gen = root / "generations" / "one"
    gen.mkdir(parents=True)
    (root / "current").symlink_to("generations/one")
    (gen / "generation.json").write_text(json.dumps({"owner": inputs.SOURCE_OWNER, "state": "degraded", "published": True}))
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    times = pd.DatetimeIndex([now - pd.Timedelta(minutes=1), now])
    for name, variable in [("power", "BatterySOC"), ("pdu", "Load")]:
        ds = xr.Dataset({variable: ("time", [90.0, 91.0])}, coords={"time": times})
        ds.to_zarr(gen / "products/power" / f"{name}.zarr", mode="w", consolidated=True)
    monkeypatch.setattr(inputs.os, "statvfs", lambda p: SimpleNamespace(f_bavail=30 * 1024**3, f_frsize=1))
    uas = tmp_path / "uas.log"
    uas.write_text(now.strftime("%Y-%m-%d %H:%M:%S") + ": 1 2\n")
    return root, gen, tmp_path / "inputs"


def test_snapshot_survives_source_retirement_and_records_actual_anchor(generation):
    root, gen, destination = generation
    snapshot = inputs.prepare(root, destination)
    metadata = json.loads((snapshot / "selection.json").read_text())
    shutil.rmtree(gen)
    result = inputs.inspect_samples(snapshot / "power.zarr", power=True)
    assert result["battery_soc"] == 91
    assert result["latest_sample_utc"] == metadata["samples"]["power"]["latest_sample_utc"]
    assert metadata["source_generation"] == str(gen)
    inputs.cleanup(snapshot)
    assert not snapshot.exists()
    assert (destination / "last-selection.json").exists()


@pytest.mark.parametrize("kind", ["stale", "future", "fresh_nan", "bad_soc"])
def test_rejects_invalid_actual_soc_despite_recent_files(generation, kind):
    root, gen, destination = generation
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    times = [now - pd.Timedelta(hours=1), now]
    values = [80.0, 81.0]
    if kind == "stale": times[-1] = now - pd.Timedelta(minutes=30)
    if kind == "future": times[-1] = now + pd.Timedelta(minutes=5)
    if kind == "fresh_nan": values[-1] = np.nan
    if kind == "bad_soc": values[-1] = 101
    xr.Dataset({"BatterySOC": ("time", values)}, coords={"time": times}).to_zarr(gen / "products/power/power.zarr", mode="w", consolidated=True)
    with pytest.raises(ValueError): inputs.prepare(root, destination)
    assert not list(destination.glob("run-*"))


def test_reserve_and_size_caps_prevent_copy(generation, monkeypatch):
    root, _, destination = generation
    monkeypatch.setattr(inputs.os, "statvfs", lambda p: SimpleNamespace(f_bavail=inputs.RESERVE_BYTES, f_frsize=1))
    with pytest.raises(ValueError, match="reserve"): inputs.prepare(root, destination)
    monkeypatch.setattr(inputs, "MAX_BYTES", 1)
    with pytest.raises(ValueError, match="512 MiB"): inputs.prepare(root, destination)
    assert not list(destination.glob("run-*"))


def test_concurrent_space_consumption_prevents_selected_snapshot(generation, monkeypatch):
    root, _, destination = generation
    readings = iter([30 * 1024**3, 9 * 1024**3])
    monkeypatch.setattr(inputs.os, "statvfs", lambda p: SimpleNamespace(f_bavail=next(readings), f_frsize=1))
    with pytest.raises(ValueError, match="while copying"): inputs.prepare(root, destination)
    assert not list(destination.glob("run-*"))
    assert not (destination / "last-selection.json").exists()


def test_unpublished_or_escaping_generation_rejected(generation):
    root, gen, destination = generation
    (gen / "generation.json").write_text('{}')
    with pytest.raises(ValueError, match="published"): inputs.prepare(root, destination)
    (root / "current").unlink()
    (root / "current").symlink_to(root.parent)
    with pytest.raises(ValueError, match="escapes"): inputs.prepare(root, destination)


def test_changed_source_and_symlinks_fail_closed(generation, monkeypatch):
    root, gen, destination = generation
    target = gen / "products/power/power.zarr/extra"
    target.symlink_to(gen / "generation.json")
    with pytest.raises(ValueError, match="symlinks"): inputs.prepare(root, destination)
    target.unlink()
    real = inputs.shutil.copytree
    def altered(source, dest, *args, **kwargs):
        result = real(source, dest, *args, **kwargs)
        if Path(source).name == "power.zarr":
            (Path(source) / "unexpected").write_text('changed')
        return result
    monkeypatch.setattr(inputs.shutil, "copytree", altered)
    with pytest.raises(ValueError, match="changed"): inputs.prepare(root, destination)
    assert not list(destination.glob("run-*"))


def test_only_owned_abandoned_snapshots_are_cleaned(generation):
    root, _, destination = generation
    first = inputs.prepare(root, destination)
    foreign = destination / "run-foreign"
    foreign.mkdir()
    (foreign / "important").write_text('preserve')
    second = inputs.prepare(root, destination)
    assert not first.exists() and second.exists() and foreign.exists()
    with pytest.raises(FileNotFoundError): inputs.cleanup(foreign)


def test_stage_refresh_keeps_original_provenance_before_retiring_snapshot(generation, monkeypatch):
    from datetime import datetime, timedelta, timezone
    root, gen, destination = generation
    uas = root.parent / "uas.log"
    forecast = inputs.prepare(root, destination, uas)
    original = json.loads((forecast / "selection.json").read_text())
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    class LaterDatetime(datetime):
        @classmethod
        def now(cls, tz=None): return later if tz else later.replace(tzinfo=None)
    monkeypatch.setattr(inputs, "datetime", LaterDatetime)
    # A long ENS calculation must not make old observations acceptable.
    with pytest.raises(ValueError, match="stale"):
        inputs.inspect_samples(forecast / "power.zarr", power=True)
    fresh_time = pd.Timestamp(later - timedelta(seconds=30)).tz_localize(None)
    for name, variable in [("power", "BatterySOC"), ("pdu", "Load")]:
        xr.Dataset({variable: ("time", [83.0])}, coords={"time": [fresh_time]}).to_zarr(
            gen / "products/power" / f"{name}.zarr", mode="w", consolidated=True)
    uas.write_text(fresh_time.strftime("%Y-%m-%d %H:%M:%S") + ": 1 2\n")
    planning = inputs.prepare(root, destination, uas, phase="planning", previous_selection=forecast / "selection.json")
    assert not forecast.exists()
    publication = inputs.prepare(root, destination, uas, phase="publication", previous_selection=planning / "selection.json")
    assert not planning.exists()
    selected = json.loads((publication / "selection.json").read_text())
    assert [row["phase"] for row in selected["prior_selections"]] == ["forecast", "planning"]
    assert selected["prior_selections"][0]["samples"] == original["samples"]
    assert selected["prior_selections"][0]["selected_at"] == original["selected_at"]
    assert selected["samples"]["power"]["battery_soc"] == 83
    assert selected["samples"]["power"]["age_seconds"] == 30


def test_stage_failure_does_not_publish_new_selection(generation, monkeypatch):
    root, _, destination = generation
    forecast = inputs.prepare(root, destination)
    before = (destination / "last-selection.json").read_bytes()
    monkeypatch.setattr(inputs.shutil, "copytree", lambda *a, **kw: (_ for _ in ()).throw(OSError("copy failed")))
    with pytest.raises(OSError, match="copy failed"):
        inputs.prepare(root, destination, phase="publication", previous_selection=forecast / "selection.json")
    assert (destination / "last-selection.json").read_bytes() == before
    assert not list(destination.glob("run-*"))


@pytest.mark.parametrize("phase,previous_kind", [("planning", "missing"), ("publication", "missing"), ("forecast", "same"), ("planning", "foreign"), ("publication", "symlink")])
def test_stage_history_requires_owned_prior_snapshot_and_valid_transition(generation, phase, previous_kind):
    root, _, destination = generation
    forecast = inputs.prepare(root, destination)
    previous = forecast / "selection.json"
    if previous_kind == "missing": previous = None
    if previous_kind == "foreign":
        foreign = root.parent / "foreign"
        shutil.copytree(forecast, foreign)
        previous = foreign / "selection.json"
    if previous_kind == "symlink":
        original = previous.read_text()
        previous.unlink()
        (forecast / "target.json").write_text(original)
        previous.symlink_to("target.json")
    with pytest.raises(ValueError):
        inputs.prepare(root, destination, phase=phase, previous_selection=previous)
    assert forecast.exists()


def test_uas_snapshot_keeps_both_docks_and_rejects_stale_or_oversized_log(generation, monkeypatch):
    root, _, destination = generation
    uas = root.parent / "uas.log"
    snapshot = inputs.prepare(root, destination, uas)
    assert (snapshot / "menapia_mqtt.log").read_bytes() == uas.read_bytes()
    assert ": 1 2" in (snapshot / "menapia_mqtt.log").read_text()
    assert "uas" in json.loads((snapshot / "selection.json").read_text())["samples"]
    uas.write_text("2026-01-01 00:00:00: 1 2\n")
    with pytest.raises(ValueError, match="stale"): inputs.prepare(root, destination, uas)
    monkeypatch.setattr(inputs, "MAX_UAS_BYTES", 1)
    with pytest.raises(ValueError, match="64 MiB"): inputs.prepare(root, destination, uas)


def test_publisher_keeps_canonical_defaults_and_requires_matching_snapshot(generation, monkeypatch):
    root, _, destination = generation
    namespace = {"__name__": "test_publisher"}
    exec(compile(_render_publisher(), "publisher", "exec"), namespace)
    assert namespace["publication_input_paths"]() == (namespace["POWER_ZARR"], namespace["PDU_ZARR"], namespace["UAS_LOG"], None)
    monkeypatch.setenv("AURORA_POWER_INPUT_MODE", "continuity")
    with pytest.raises(KeyError): namespace["publication_input_paths"]()
    snapshot = inputs.prepare(root, destination, root.parent / "uas.log")
    for key, value in {"AURORA_POWER_SELECTED_UAS_LOG": snapshot / "menapia_mqtt.log", "AURORA_POWER_INPUT_SELECTION": snapshot / "selection.json", "AURORA_POWER_SELECTED_POWER_ZARR": snapshot / "power.zarr", "AURORA_POWER_SELECTED_PDU_ZARR": snapshot / "pdu.zarr"}.items():
        monkeypatch.setenv(key, str(value))
    assert namespace["publication_input_paths"]() == (snapshot / "power.zarr", snapshot / "pdu.zarr", snapshot / "menapia_mqtt.log", snapshot / "selection.json")
    monkeypatch.setenv("AURORA_POWER_SELECTED_PDU_ZARR", str(root / "products/pdu.zarr"))
    with pytest.raises(ValueError, match="share"): namespace["publication_input_paths"]()


@pytest.mark.parametrize("action", ["full-cycle", "cached-reanchor-condition", "immutable-ensemble"])
@pytest.mark.parametrize("mode,fail_phase", [("canonical", ""), ("continuity", ""), ("continuity", "forecast"), ("continuity", "planning"), ("continuity", "publication")])
def test_runner_freezes_forecast_pair_refreshes_stages_and_cleans_up(generation, tmp_path, action, mode, fail_phase):
    root, _, destination = generation
    bridge = tmp_path / "python-bridge"
    log = tmp_path / "calls.jsonl"
    bridge.write_text(f'''#!{sys.executable}
import json,os,subprocess,sys
from pathlib import Path
args=sys.argv[1:]
if args[0].endswith("aurora-power-forecast-inputs.py"):
    if args[1] == "prepare" and args[args.index("--phase")+1] == os.environ.get("FAIL_PHASE"): sys.exit(1)
    sys.exit(subprocess.call([sys.executable]+args))
selection=Path(os.environ.get("AURORA_POWER_INPUT_SELECTION", "missing"))
with open(os.environ["CALL_LOG"],"a") as f:
    f.write(json.dumps({{"args":args,"power":os.environ.get("AURORA_POWER_SELECTED_POWER_ZARR"),"pdu":os.environ.get("AURORA_POWER_SELECTED_PDU_ZARR"),"selection":json.loads(selection.read_text()) if selection.is_file() else None}})+"\\n")
if "--power-zarr" in args and os.environ["AURORA_POWER_INPUT_MODE"]=="continuity":
    assert Path(args[args.index("--power-zarr")+1]).is_dir()
if "--print-issue-anchor" in args or "--print-forecast-anchor" in args:
    print("2026-10-23T07:32:51Z")
''')
    bridge.chmod(0o755)
    script = _render(FORECAST_RUNNER)
    script = script.replace("python=/opt/aurora-cloud-dashboard/venv/bin/python", "python=" + str(bridge))
    script = script.replace("app_dir=/opt/aurora-cloud-dashboard", "app_dir=" + str(tmp_path))
    script = script.replace("/data/aurora/dev-products/power/.deterministic-generator.lock", str(tmp_path / "lock"))
    script = script.replace("/usr/bin/flock", "/usr/bin/true")
    for duration in ("2h", "15m", "20m"):
        script = script.replace("/usr/bin/timeout --signal=TERM --kill-after=30s " + duration, "")
    runner = tmp_path / "runner.sh"
    runner.write_text(script)
    environment = dict(os.environ, AURORA_POWER_INPUT_MODE=mode, AURORA_POWER_CONTINUITY_ROOT=str(root), AURORA_POWER_INPUT_ROOT=str(destination), AURORA_POWER_INPUT_HELPER=str(HELPER), AURORA_POWER_UAS_LOG=str(tmp_path / "uas.log"), CALL_LOG=str(log), FAIL_PHASE=fail_phase)
    result = subprocess.run(["bash", str(runner), action], env=environment, capture_output=True, text=True)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    phases = {"full-cycle": ["forecast", "planning", "publication"], "cached-reanchor-condition": ["forecast", "publication"], "immutable-ensemble": ["forecast"]}[action]
    commands = [row for row in calls if "--power-zarr" in row["args"]]
    if fail_phase in phases:
        assert result.returncode == 255
        assert all("--publish" not in row["args"] for row in calls)
        assert len(commands) == {"forecast": 0, "planning": 2, "publication": 3 if action == "full-cycle" else 2}[fail_phase]
        assert not list(destination.glob("run-*"))
        return
    assert result.returncode == 0, result.stderr
    assert len(commands) == {"full-cycle": 3, "cached-reanchor-condition": 2, "immutable-ensemble": 1}[action]
    used = {row["args"][row["args"].index("--power-zarr") + 1] for row in commands}
    assert len(used) == (2 if mode == "continuity" and action == "full-cycle" else 1)
    for row in commands:
        if "generate_power_soc_forecast.py" in row["args"]:
            expected_pdu = row["pdu"] if mode == "continuity" else "/data/aurora/products/power/pdu.zarr"
            assert row["args"][row["args"].index("--pdu-zarr") + 1] == expected_pdu
    if mode == "continuity":
        assert all(Path(path).parent.parent == destination for path in used)
        assert all(row["pdu"] == str(Path(row["power"]).parent / "pdu.zarr") for row in calls)
        assert commands[0]["selection"]["phase"] == "forecast"
        if action != "immutable-ensemble":
            assert commands[0]["power"] == commands[1]["power"]
            published = next(row for row in calls if "--publish" in row["args"])
            assert published["selection"]["phase"] == "publication"
            assert published["power"] not in used
            chain = published["selection"]["prior_selections"] + [published["selection"]]
            assert [row["phase"] for row in chain] == phases
            assert chain[0]["snapshot"] == str(Path(commands[0]["power"]).parent)
        if action == "full-cycle":
            assert commands[2]["selection"]["phase"] == "planning"
            assert commands[2]["power"] != commands[0]["power"]
        assert not list(destination.glob("run-*"))
    else:
        assert used == {"/data/aurora/products/power/power.zarr"}
        assert not destination.exists()
