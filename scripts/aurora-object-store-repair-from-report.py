#!/usr/bin/env python3
"""Repair exact source/S3 differences from the latest comparison report.

This is intentionally copy-only. It never removes objects that exist only on
S3. Paths are taken from the settled source snapshot in comparison.json and
are rechecked for settle age before transfer.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile


DEFAULT_REPORT = Path(
    "/data/aurora/internal/object_store_manifests/latest/comparison.json"
)
RCLONE = "/usr/bin/rclone"
RCLONE_CONFIG = "/etc/aurora-object-store/rclone.conf"
REMOTE = "gamb2le-object-store:gamb2le-o"

JOBS = {
    "raw": {
        "source": Path("/project/aurora/raw"),
        "destination": "data/incoming/aurora-cloud/raw",
        "settle_seconds": 15 * 60,
    },
    "products": {
        "source": Path("/data/aurora/products"),
        "destination": "data/output/aurora-cloud/products",
        "settle_seconds": 20 * 60,
    },
    "products-wxcam": {
        "source": Path("/data/aurora/products/wxcam"),
        "destination": "data/output/aurora-cloud/products/wxcam",
        "settle_seconds": 30 * 60,
    },
    "model-evaluation": {
        "source": Path(
            "/data/aurora/model-evaluation/campaigns/"
            "aurora_iceland_model_evaluation_v1"
        ),
        "destination": "data/output/aurora-cloud/model-evaluation",
        "settle_seconds": 60 * 60,
        "copy_links": True,
    },
    "manifests": {
        "source": Path("/data/aurora/internal/mirror_manifests"),
        "destination": "data/internal/aurora-cloud/manifests/gws",
        "settle_seconds": 5 * 60,
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--job",
        action="append",
        choices=sorted(JOBS),
        help="Repair only this job; may be repeated.",
    )
    return parser.parse_args()


def settled_paths(
    source: Path, paths: set[str], settle_seconds: int
) -> tuple[list[str], list[str]]:
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - settle_seconds
    ready: list[str] = []
    deferred: list[str] = []
    for relative_path in sorted(paths):
        path = source / relative_path
        try:
            stat = path.stat()
        except FileNotFoundError:
            deferred.append(relative_path)
            continue
        if stat.st_mtime > cutoff:
            deferred.append(relative_path)
            continue
        ready.append(relative_path)
    return ready, deferred


def repair_job(name: str, report_job: dict, dry_run: bool) -> dict:
    config = JOBS[name]
    comparison = report_job["source_vs_s3"]
    candidates = set(comparison.get("missing_from_right", []))
    candidates.update(comparison.get("size_mismatch", []))
    candidates.update(comparison.get("checksum_mismatch", []))
    ready, deferred = settled_paths(
        config["source"], candidates, config["settle_seconds"]
    )
    result = {
        "job": name,
        "candidates": len(candidates),
        "ready": len(ready),
        "deferred": len(deferred),
        "returncode": 0,
    }
    if not ready:
        return result

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=f"{name}-repair-", delete=True
    ) as file_list:
        file_list.write("\n".join(ready) + "\n")
        file_list.flush()
        command = [
            RCLONE,
            "copy",
            f"{config['source']}/",
            f"{REMOTE}/{config['destination']}/",
            f"--config={RCLONE_CONFIG}",
            f"--files-from-raw={file_list.name}",
            "--no-traverse",
            "--ignore-times",
            "--checkers=8",
            "--transfers=4",
            "--s3-upload-concurrency=4",
            "--s3-chunk-size=64M",
            "--contimeout=30s",
            "--timeout=10m",
            "--retries=4",
            "--low-level-retries=10",
            "--stats=1m",
        ]
        if config.get("copy_links"):
            command.append("--copy-links")
        if dry_run:
            command.append("--dry-run")
        completed = subprocess.run(command, check=False)
        result["returncode"] = completed.returncode
    return result


def main() -> int:
    args = parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    selected = set(args.job or JOBS)
    results = []
    for name in JOBS:
        if name not in selected:
            continue
        results.append(repair_job(name, report["jobs"][name], args.dry_run))
    print(json.dumps({"report": report["generated_at"], "jobs": results}, indent=2))
    return 1 if any(item["returncode"] for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
