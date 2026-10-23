#!/usr/bin/env python3
"""Snapshot published continuity Power/PDU and optional UAS for a forecast run."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

OWNER = "aurora-power-forecast-inputs-v1"
SOURCE_OWNER = "aurora-live-continuity-v1-20261021"
MAX_BYTES = 512 * 1024**2
RESERVE_BYTES = 10 * 1024**3
MAX_UAS_BYTES = 64 * 1024**2


def cleanup(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("snapshot must be an owned directory")
    if (path / ".owner").read_text().strip() != OWNER:
        raise ValueError("snapshot ownership mismatch")
    shutil.rmtree(path)


def inventory(path: Path) -> dict[str, tuple[int, int]]:
    if path.is_symlink():
        raise ValueError("input store must not be a symlink")
    result = {}
    for item in path.rglob("*"):
        if item.is_symlink():
            raise ValueError("symlinks are not permitted inside input stores")
        if item.is_file():
            info = item.stat()
            result[str(item.relative_to(path))] = (info.st_size, info.st_mtime_ns)
    if not result or len(result) > 20000:
        raise ValueError("empty or oversized input file inventory")
    return result


def inspect_samples(path: Path, *, power: bool, now=None) -> dict:
    import numpy as np
    import pandas as pd
    import xarray as xr

    reference = pd.Timestamp(now or datetime.now(timezone.utc)).tz_convert("UTC").tz_localize(None)
    with xr.open_zarr(path, chunks=None) as ds:
        times = np.asarray(ds.time.values, dtype="datetime64[ns]")
        if not len(times) or np.isnat(times).any() or (np.diff(times) < np.timedelta64(0, "ns")).any():
            raise ValueError("input times must be populated, finite and ordered")
        if power:
            values = np.asarray(ds["BatterySOC"].values, dtype=float)
            valid = np.isfinite(values)
            if values.ndim != 1 or not valid.any():
                raise ValueError("no finite BatterySOC sample")
            stamp = pd.Timestamp(times[valid][-1])
            soc = float(values[valid][-1])
            if not 0 <= soc <= 100:
                raise ValueError("BatterySOC lies outside 0..100")
        else:
            stamp = pd.Timestamp(times[-1])
            finite = any(np.issubdtype(ds[k].dtype, np.number) and np.isfinite(ds[k].isel(time=-1).values).any()
                         for k in ds.data_vars if "time" in ds[k].dims)
            if not finite:
                raise ValueError("latest PDU sample has no finite measurement")
            soc = None
        age = (reference - stamp).total_seconds()
        if age > 1200 or age < 0:
            raise ValueError(f"{'Power' if power else 'PDU'} sample is stale or future-dated: {age:.1f}s")
        return {"latest_sample_utc": stamp.isoformat() + "Z", "age_seconds": age,
                "rows": len(times), "battery_soc": soc}


def snapshot_uas(source: Path, destination: Path) -> dict:
    if source.is_symlink():
        raise ValueError("UAS source must be a regular atomic-published file")
    with source.open("rb") as src, destination.open("wb") as dst:
        before = os.fstat(src.fileno())
        if not 0 < before.st_size <= MAX_UAS_BYTES:
            raise ValueError("UAS snapshot must be between 1 byte and 64 MiB")
        copied = 0
        while chunk := src.read(min(1024**2, MAX_UAS_BYTES + 1 - copied)):
            copied += len(chunk)
            if copied > MAX_UAS_BYTES:
                raise ValueError("UAS source exceeded 64 MiB while copying")
            dst.write(chunk)
        after = os.fstat(src.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("UAS source changed in place during snapshot")
    latest = None
    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):\s+(?:Tier change\s+)?-?\d+\s+-?\d+\s*$")
    with destination.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = pattern.match(line.strip())
            if match:
                stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                latest = max(latest, stamp) if latest else stamp
    if latest is None:
        raise ValueError("UAS snapshot has no parsed sample")
    age = (datetime.now(timezone.utc) - latest).total_seconds()
    if age > 1200 or age < 0:
        raise ValueError("UAS sample is stale or future-dated")
    return {"latest_sample_utc": latest.isoformat(), "age_seconds": age, "bytes": before.st_size}


def prior_selections(input_root: Path, phase: str, previous: Path | None) -> list[dict]:
    """Read bounded stage provenance before the prior owned snapshot is retired."""
    if phase not in ("forecast", "planning", "publication"):
        raise ValueError("unknown input phase")
    if previous is None:
        if phase != "forecast":
            raise ValueError("later input phases require the previous selection")
        return []
    if (previous.is_symlink() or previous.name != "selection.json"
            or previous.parent.is_symlink()
            or previous.parent.resolve().parent != input_root.resolve()
            or not previous.parent.name.startswith("run-")
            or (previous.parent / ".owner").read_text().strip() != OWNER
            or previous.stat().st_size > 65536):
        raise ValueError("previous selection must belong to this snapshot root")
    record = json.loads(previous.read_text())
    if (record.get("owner") != OWNER or record.get("mode") != "continuity"
            or Path(record.get("snapshot", "")).resolve() != previous.parent.resolve()):
        raise ValueError("previous selection ownership mismatch")
    history = record.get("prior_selections", [])
    old_phase = record.get("phase")
    if not isinstance(history, list) or [row.get("phase") for row in history] != ([] if old_phase == "forecast" else ["forecast"]):
        raise ValueError("invalid previous selection history")
    if (phase, old_phase) not in (("planning", "forecast"), ("publication", "forecast"), ("publication", "planning")):
        raise ValueError("invalid input phase transition")
    record.pop("prior_selections", None)
    return history + [record]


def prepare(source_root: Path, input_root: Path, uas_log: Path | None = None,
            *, phase: str = "forecast", previous_selection: Path | None = None) -> Path:
    source_root = source_root.resolve(strict=True)
    pointer = source_root / "current"
    if not pointer.is_symlink():
        raise ValueError("continuity current must be a symlink")
    generation = pointer.resolve(strict=True)
    if generation.parent != source_root / "generations":
        raise ValueError("continuity generation escapes its owner root")
    record_path = generation / "generation.json"
    record_text = record_path.read_text()
    record = json.loads(record_text)
    if record.get("owner") != SOURCE_OWNER or not record.get("published"):
        raise ValueError("continuity generation is not an owned published generation")
    if record.get("state") not in ("complete", "degraded"):
        raise ValueError("continuity generation is incomplete")
    if input_root.is_symlink():
        raise ValueError("input root must not be a symlink")
    input_root.mkdir(parents=True, exist_ok=True)
    if input_root.resolve().is_relative_to(source_root):
        raise ValueError("forecast snapshots must be outside continuity source")
    history = prior_selections(input_root, phase, previous_selection)
    # The caller holds the existing forecast producer lock throughout this helper
    # and every consumer. Reclaim only prior snapshots with our exact marker.
    for prior in input_root.glob("run-*"):
        if prior.is_dir() and not prior.is_symlink() and (prior / ".owner").is_file():
            if (prior / ".owner").read_text().strip() == OWNER:
                cleanup(prior)
    sources = {name: generation / "products" / "power" / f"{name}.zarr" for name in ("power", "pdu")}
    inventories = {name: inventory(path) for name, path in sources.items()}
    total = sum(size for rows in inventories.values() for size, _ in rows.values())
    required = total
    if uas_log is not None:
        uas_size = uas_log.stat().st_size
        if not 0 < uas_size <= MAX_UAS_BYTES:
            raise ValueError("UAS snapshot must be between 1 byte and 64 MiB")
        total += uas_size
        # The separately atomically published log can be replaced between this
        # stat and open. Reserve its entire bounded allowance before copying.
        required += MAX_UAS_BYTES
    if total > MAX_BYTES:
        raise ValueError("Power/PDU input snapshot exceeds 512 MiB")
    stats = os.statvfs(input_root)
    if stats.f_bavail * stats.f_frsize < RESERVE_BYTES + required:
        raise ValueError("input snapshot would breach the 10 GiB free-space reserve")
    target = Path(tempfile.mkdtemp(prefix="run-", dir=input_root))
    (target / ".owner").write_text(OWNER + "\n")
    try:
        for name, source in sources.items():
            shutil.copytree(source, target / f"{name}.zarr")
            if inventory(source) != inventories[name] or inventory(target / f"{name}.zarr") != inventories[name]:
                raise ValueError("source or copied input changed during snapshot")
        if record_path.read_text() != record_text:
            raise ValueError("source generation metadata changed during snapshot")
        samples = {name: inspect_samples(target / f"{name}.zarr", power=name == "power") for name in sources}
        if uas_log is not None:
            samples["uas"] = snapshot_uas(uas_log, target / "menapia_mqtt.log")
            total += samples["uas"]["bytes"] - uas_size
            if total > MAX_BYTES:
                raise ValueError("Power/PDU/UAS input snapshot exceeds 512 MiB")
        stats = os.statvfs(input_root)
        if stats.f_bavail * stats.f_frsize < RESERVE_BYTES:
            raise ValueError("free space fell below the 10 GiB reserve while copying inputs")
        info = {"owner": OWNER, "mode": "continuity", "phase": phase,
                "prior_selections": history, "selected_at": datetime.now(timezone.utc).isoformat(),
                "source_generation": str(generation), "uas_source": str(uas_log) if uas_log else None,
                "snapshot": str(target), "bytes": total, "samples": samples}
        (target / "selection.json").write_text(json.dumps(info, indent=2) + "\n")
        tmp = input_root / "last-selection.json.tmp"
        tmp.write_text(json.dumps(info, indent=2) + "\n")
        tmp.replace(input_root / "last-selection.json")
        return target
    except BaseException:
        cleanup(target)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    command = sub.add_parser("prepare")
    command.add_argument("--source-root", type=Path, required=True)
    command.add_argument("--input-root", type=Path, required=True)
    command.add_argument("--uas-log", type=Path)
    command.add_argument("--phase", choices=("forecast", "planning", "publication"), default="forecast")
    command.add_argument("--previous-selection", type=Path)
    command = sub.add_parser("cleanup")
    command.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "cleanup":
            cleanup(args.snapshot)
        else:
            print(prepare(args.source_root, args.input_root, args.uas_log,
                          phase=args.phase, previous_selection=args.previous_selection))
        return 0
    except Exception as exc:
        print(f"Power forecast inputs rejected: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
