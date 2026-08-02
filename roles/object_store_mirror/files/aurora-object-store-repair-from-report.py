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
DEFAULT_CATALOG = Path("/etc/aurora-object-store/catalog.json")
RCLONE = "/usr/bin/rclone"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--job",
        action="append",
        help="Repair only this catalogue job; may be repeated.",
    )
    return parser.parse_args()


def duration_seconds(value: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(value[:-1]) * units[value[-1].lower()]


def verification_settle_age(job: dict) -> str:
    return job.get(
        "verification_settle_age",
        job.get("settle_age", "15m"),
    )


def settled_paths(
    source: Path,
    paths: set[str],
    settle_seconds: int,
    copy_links: bool = False,
) -> tuple[list[str], list[str]]:
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - settle_seconds
    ready_with_mtime: list[tuple[float, str]] = []
    deferred: list[str] = []
    for relative_path in paths:
        path = source / relative_path
        try:
            path.resolve(strict=False).relative_to(source.resolve())
            stat = path.stat()
        except (FileNotFoundError, ValueError):
            deferred.append(relative_path)
            continue
        if (
            (path.is_symlink() and not copy_links)
            or not path.is_file()
            or stat.st_mtime > cutoff
        ):
            deferred.append(relative_path)
            continue
        ready_with_mtime.append((stat.st_mtime, relative_path))
    ready = [
        relative
        for _mtime, relative in sorted(
            ready_with_mtime,
            key=lambda item: (-item[0], item[1]),
        )
    ]
    return ready, sorted(deferred)


def repair_job(
    name: str,
    job: dict,
    report_job: dict,
    catalog: dict,
    dry_run: bool,
) -> dict:
    comparison = report_job["source_vs_s3"]
    candidates = set(comparison.get("missing_from_right", []))
    candidates.update(comparison.get("size_mismatch", []))
    candidates.update(comparison.get("checksum_mismatch", []))
    source = Path(job["source"])
    ready, deferred = settled_paths(
        source,
        candidates,
        duration_seconds(verification_settle_age(job)),
        bool(job.get("copy_links")),
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
            f"{source}/",
            (
                f"{catalog['remote']}:{catalog['bucket']}/"
                f"{job['destination'].strip('/')}/"
            ),
            f"--config={catalog['rclone_config']}",
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
        if job.get("copy_links"):
            command.append("--copy-links")
        if dry_run:
            command.append("--dry-run")
        completed = subprocess.run(command, check=False)
        result["returncode"] = completed.returncode
    return result


def main() -> int:
    args = parse_args()
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    report = json.loads(args.report.read_text(encoding="utf-8"))
    jobs = {job["name"]: job for job in catalog["jobs"]}
    selected = set(args.job or jobs)
    unknown = selected - set(jobs)
    if unknown:
        raise SystemExit(f"unknown catalogue jobs: {', '.join(sorted(unknown))}")
    results = []
    for name, job in jobs.items():
        if name not in selected:
            continue
        results.append(
            repair_job(
                name,
                job,
                report["jobs"][name],
                catalog,
                args.dry_run,
            )
        )
    print(json.dumps({"report": report["generated_at"], "jobs": results}, indent=2))
    return 1 if any(item["returncode"] for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
