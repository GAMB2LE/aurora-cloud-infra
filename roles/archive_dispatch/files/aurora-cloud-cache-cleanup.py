#!/usr/bin/env python3
"""Remove the rebuildable WXcam pixel cache behind strict archive guards."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess


TARGET = Path("/data/aurora/products/wxcam/wxcam.zarr")
HEALTH = Path("/data/aurora/internal/archive_status/health-v1.json")
AUDIT_ROOT = Path("/data/aurora/internal/cache_retention")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected object in {path}")
    return value


def require_archive_clean(health: dict) -> None:
    if health.get("overall_level") != "green" or health.get("failures"):
        raise RuntimeError("fresh strict archive verification is not green")
    metrics = health.get("metrics")
    if not isinstance(metrics, dict):
        raise RuntimeError("archive metrics are unavailable")
    for key in (
        "streams_gws_issue_count",
        "object_store_raw_missing_count",
        "object_store_raw_mismatch_count",
    ):
        if int(metrics.get(key, -1)) != 0:
            raise RuntimeError(f"archive guard failed: {key}={metrics.get(key)}")
    evidence = health.get("evidence")
    if not isinstance(evidence, dict):
        raise RuntimeError("archive evidence is unavailable")
    gate = evidence.get("object_store_gate")
    if not isinstance(gate, dict) or not gate.get("stable_parity"):
        raise RuntimeError("object-store stable parity is not established")


def timer_disabled() -> bool:
    completed = subprocess.run(
        ["systemctl", "is-enabled", "aurora-wxcam-append.timer"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return completed.stdout.strip() in {"disabled", "masked", "not-found"}


def staged_targets(target: Path = TARGET) -> list[Path]:
    return sorted(target.parent.glob(f"{target.name}.deleting-*"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    health = read(HEALTH)
    require_archive_clean(health)
    if not timer_disabled():
        raise RuntimeError("aurora-wxcam-append.timer must be disabled first")
    staged = staged_targets()
    if not TARGET.exists() and not staged:
        print(json.dumps({"state": "already_absent", "target": str(TARGET)}))
        return 0
    if TARGET.exists() and (not TARGET.is_dir() or TARGET.is_symlink()):
        raise RuntimeError(f"refusing unexpected cache target: {TARGET}")
    if TARGET.exists() and staged:
        raise RuntimeError(
            "refusing cleanup while both the active target and a staged "
            "deletion are present"
        )
    if len(staged) > 1:
        raise RuntimeError("refusing multiple staged WXcam cache deletions")
    if staged and (not staged[0].is_dir() or staged[0].is_symlink()):
        raise RuntimeError(f"refusing unexpected staged target: {staged[0]}")

    receipt = {
        "schema_version": 1,
        "started_at": now(),
        "target": str(TARGET),
        "reason": "rebuildable WXcam pixel cache; canonical HDR media are dual archived",
        "archive_health_generated_at": health.get("generated_at"),
        "object_store_run": health.get("evidence", {}).get(
            "object_store_generated_at"
        ),
        "mode": "apply" if args.apply else "dry-run",
        "state": "eligible_resume" if staged else "eligible",
    }
    AUDIT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = receipt["started_at"].replace(":", "").replace("-", "")
    path = AUDIT_ROOT / f"wxcam-pixel-zarr-{stamp}.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    if args.apply:
        staged_target = (
            staged[0]
            if staged
            else TARGET.with_name(f"{TARGET.name}.deleting-{stamp}")
        )
        if TARGET.exists():
            TARGET.rename(staged_target)
        receipt["staged_target"] = str(staged_target)
        receipt["state"] = "deleting"
        path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        try:
            shutil.rmtree(staged_target)
        except Exception as error:
            receipt["state"] = "failed"
            receipt["error"] = f"{type(error).__name__}: {error}"
            receipt["finished_at"] = now()
            path.write_text(
                json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
            )
            raise
        else:
            receipt["state"] = "removed"
            receipt["finished_at"] = now()
    path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
