#!/usr/bin/env python3
"""Build reproducible cloud/GWS/S3 comparison evidence from the catalogue."""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import fnmatch
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

CATALOG_PATH = Path("/etc/aurora-object-store/catalog.json")
COMMON_EXCLUDES = [
    "**/.git/**",
    "**/.venv/**",
    "**/__pycache__/**",
    "**/.cache/**",
    "**/*.lock",
    "**/*.partial",
    "**/*.part",
    "**/*.tmp",
    "**/*-wal",
    "**/*-shm",
    "**/logs/**",
    "**/*backup*.zarr/**",
    "**/*schema-backup*.zarr/**",
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def duration_seconds(value: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(value[:-1]) * units[value[-1].lower()]


def excluded(path: str, patterns: list[str]) -> bool:
    value = path.lstrip("/")
    return any(
        fnmatch.fnmatch(value, pattern.lstrip("/"))
        or fnmatch.fnmatch(value, pattern.lstrip("/").replace("**/", "*/"))
        for pattern in patterns
    )


def local_inventory(root: str, patterns: list[str], settle_age: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    base = Path(root)
    if not base.exists():
        return result
    settled_before = time.time() - duration_seconds(settle_age)
    for directory, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in {".git", ".venv", "__pycache__", ".cache"}
            and not name.endswith((".partial", ".tmp"))
        ]
        for name in filenames:
            path = Path(directory, name)
            try:
                # Symlinks are operational pointers, not independently
                # restorable archive objects. The repair service deliberately
                # refuses them, so they must not enter the parity contract.
                if path.is_symlink():
                    continue
                stat = path.stat()
            except FileNotFoundError:
                continue
            if stat.st_mtime > settled_before:
                continue
            relative = path.relative_to(base).as_posix()
            if excluded(relative, patterns):
                continue
            result[relative] = {
                "relative_path": relative,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "checksum": "",
            }
    return result


def retain_unchanged_local_snapshot(
    root: str, rows: dict[str, dict]
) -> dict[str, dict]:
    """Keep only files unchanged for the entire remote inventory window."""
    base = Path(root)
    stable: dict[str, dict] = {}
    for relative, row in rows.items():
        path = base / relative
        try:
            if path.is_symlink():
                continue
            stat = path.stat()
        except FileNotFoundError:
            continue
        if (
            stat.st_size == row["size"]
            and int(stat.st_mtime) == int(row["mtime"])
        ):
            stable[relative] = row
    return stable


class S3Lister:
    def __init__(self, config: dict):
        self.config = config
        self.list_slots = threading.BoundedSemaphore(
            int(config.get("list_process_limit", 12))
        )

    def list_json(
        self, remote: str, *, recursive: bool = True, files_only: bool = True
    ) -> list[dict]:
        command = [
            "/usr/bin/rclone",
            "lsjson",
            f"--config={self.config['rclone_config']}",
            remote,
            "--contimeout=30s",
            "--timeout=10m",
            "--use-server-modtime",
            "--retries=6",
            "--low-level-retries=20",
        ]
        if files_only:
            command.insert(2, "--files-only")
        if recursive:
            command.insert(2, "--fast-list")
            command.insert(2, "--recursive")
        with self.list_slots:
            completed = subprocess.run(
                command,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                timeout=1800,
            )
        return json.loads(completed.stdout or "[]")

    def inventory(self, job: dict, local: dict[str, dict]) -> dict[str, dict]:
        destination = job["destination"].strip("/")
        remote = f"{self.config['remote']}:{self.config['bucket']}/{destination}"
        patterns = COMMON_EXCLUDES + job.get("exclude", [])

        def tree_excluded(path: str) -> bool:
            return excluded(path, patterns) or excluded(
                f"{path.rstrip('/')}/__inventory_probe__",
                patterns,
            )

        top_level = self.list_json(remote, recursive=False, files_only=False)
        local_prefixes = {path.split("/", 1)[0] for path in local if "/" in path}
        remote_prefixes = {
            item["Path"]
            for item in top_level
            if item.get("IsDir") and not tree_excluded(item["Path"])
        }
        prefixes = sorted(local_prefixes | remote_prefixes)
        sharded = set(job.get("sharded_prefixes", []))
        shard_all_prefixes = bool(job.get("shard_all_prefixes", False))

        def list_shallow_shards(prefix: str) -> tuple[str, list[dict]]:
            prefix_remote = f"{remote}/{prefix}"
            if prefix not in sharded and not shard_all_prefixes:
                return prefix, self.list_json(prefix_remote)

            first_level = self.list_json(
                prefix_remote, recursive=False, files_only=False
            )
            combined: list[dict] = []
            shards: list[tuple[str, str]] = []
            for parent_item in first_level:
                parent = parent_item["Path"]
                if not parent_item.get("IsDir"):
                    item = dict(parent_item)
                    item["Path"] = parent
                    combined.append(item)
                    continue
                parent_remote = f"{prefix_remote}/{parent}"
                second_level = self.list_json(
                    parent_remote, recursive=False, files_only=False
                )
                for child_item in second_level:
                    child = child_item["Path"]
                    relative = f"{parent}/{child}"
                    if child_item.get("IsDir"):
                        shards.append((relative, f"{parent_remote}/{child}"))
                    else:
                        item = dict(child_item)
                        item["Path"] = relative
                        combined.append(item)

            def list_shard(value: tuple[str, str]) -> list[dict]:
                relative, shard_remote = value
                items = self.list_json(shard_remote)
                for item in items:
                    item["Path"] = f"{relative}/{item['Path']}"
                return items

            with ThreadPoolExecutor(
                max_workers=int(
                    self.config.get(
                        "shard_list_workers",
                        self.config.get("list_workers", 3),
                    )
                )
            ) as pool:
                for items in pool.map(list_shard, shards):
                    combined.extend(items)
            return prefix, combined

        listed: list[tuple[str, list[dict]]] = []
        if prefixes:
            with ThreadPoolExecutor(
                max_workers=int(self.config.get("list_workers", 3))
            ) as pool:
                listed = list(pool.map(list_shallow_shards, prefixes))

        result: dict[str, dict] = {}
        for item in top_level:
            if not item.get("IsDir"):
                relative = item["Path"]
                if excluded(relative, patterns):
                    continue
                result[relative] = record(relative, item)
        for prefix, items in listed:
            for item in items:
                relative = f"{prefix}/{item['Path']}"
                if excluded(relative, patterns):
                    continue
                result[relative] = record(relative, item)
        return result


def record(relative: str, item: dict) -> dict:
    return {
        "relative_path": relative,
        "size": int(item.get("Size", 0)),
        "mtime": item.get("ModTime", ""),
        "checksum": "",
    }


def mirror_manifest_inventory(
    config: dict, job: dict, side: str
) -> dict[str, dict]:
    if job["name"] != "raw":
        return {}
    if side not in {"source", "gws"}:
        raise ValueError(f"unsupported mirror manifest side: {side}")
    result: dict[str, dict] = {}
    root = Path(config["gws_manifest_root"])
    latest = root / "latest"
    if not latest.exists():
        return result
    snapshot = latest
    snapshot_time = time.time()
    try:
        summary = json.loads(
            (latest / "summary.json").read_text(encoding="utf-8")
        )
        generated_at = dt.datetime.fromisoformat(
            summary["generated_at"].replace("Z", "+00:00")
        )
        run_id = generated_at.strftime("%Y%m%dT%H%M%SZ")
        history = root / "history" / run_id
        if history.exists():
            # Read both sides from the immutable history tree even if latest
            # is atomically replaced while this inventory is running.
            snapshot = history
        snapshot_time = generated_at.timestamp()
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        pass
    settled_before = snapshot_time - int(
        config.get("gws_settle_seconds", 2700)
    )
    archive_paths = {
        stream["name"]: stream["archive_relpath"]
        for stream in config.get("streams", [])
    }
    for path in snapshot.glob(f"*/{side}.tsv"):
        stream = path.parent.name
        archive_path = archive_paths.get(stream, stream).strip("/")
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                if (
                    side == "source"
                    and float(row.get("mtime") or 0) > settled_before
                ):
                    continue
                relative = f"{archive_path}/{row['relpath']}"
                result[relative] = {
                    "relative_path": relative,
                    "size": int(row["size"]),
                    "mtime": row.get("mtime", ""),
                    "checksum": row.get("checksum", ""),
                }
    return result


def gws_inventory(config: dict, job: dict) -> dict[str, dict]:
    """Return canonical GWS rows retained for callers of the v2 helper."""
    return mirror_manifest_inventory(config, job, "gws")


def compare(left: dict[str, dict], right: dict[str, dict]) -> dict:
    left_keys, right_keys = set(left), set(right)
    shared = left_keys & right_keys
    size_mismatch = sorted(
        path for path in shared if left[path]["size"] != right[path]["size"]
    )
    checksum_mismatch = sorted(
        path
        for path in shared
        if left[path].get("checksum")
        and right[path].get("checksum")
        and left[path]["checksum"] != right[path]["checksum"]
    )
    return {
        "left_count": len(left),
        "right_count": len(right),
        "missing_from_right": sorted(left_keys - right_keys),
        "extra_in_right": sorted(right_keys - left_keys),
        "size_mismatch": size_mismatch,
        "checksum_mismatch": checksum_mismatch,
        "matches": len(shared) - len(size_mismatch) - len(checksum_mismatch),
    }


def write_tsv(path: Path, rows: dict[str, dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("relative_path", "size", "mtime", "checksum"),
            delimiter="\t",
        )
        writer.writeheader()
        writer.writerows(rows[key] for key in sorted(rows))


def write_progress(root: Path, progress: dict) -> None:
    temporary = root / ".progress.tmp"
    temporary.write_text(
        json.dumps(progress, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(root / "progress.json")


def render_markdown(report: dict) -> str:
    lines = [
        "# Aurora GWS / Object-store comparison",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "| Job | Source | S3 | Missing | Size mismatch |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, values in report["jobs"].items():
        check = values["source_vs_s3"]
        lines.append(
            f"| {name} | {check['left_count']} | {check['right_count']} | "
            f"{len(check['missing_from_right'])} | {len(check['size_mismatch'])} |"
        )
    return "\n".join(lines) + "\n"


def publish(root: Path, stage: Path, generated_at: str) -> None:
    latest = root / "latest"
    history = root / "history" / generated_at.replace(":", "").replace("-", "")
    incoming = root / ".latest.new"
    if incoming.exists():
        shutil.rmtree(incoming)
    shutil.copytree(stage, incoming)
    if latest.exists():
        backup = root / ".latest.old"
        if backup.exists():
            shutil.rmtree(backup)
        latest.replace(backup)
        incoming.replace(latest)
        shutil.rmtree(backup)
    else:
        incoming.replace(latest)
    history.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(latest, history)


def main() -> int:
    config = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    generated_at = utc_now()
    root = Path(config["manifest_root"])
    root.mkdir(parents=True, exist_ok=True)
    lister = S3Lister(config)
    progress = {
        "schema_version": 1,
        "run_generated_at": generated_at,
        "updated_at": generated_at,
        "state": "running",
        "current_job": None,
        "completed_jobs": [],
        "total_jobs": len(config["jobs"]),
    }
    write_progress(root, progress)
    try:
        with tempfile.TemporaryDirectory(prefix=".inventory-", dir=root) as temporary:
            stage = Path(temporary)
            report = {
                "schema_version": 3,
                "generated_at": generated_at,
                "layout_contract": {
                    "s3": "preserves settled cloud-ingress relative paths",
                    "gws_raw": (
                        "uses canonical per-stream Y/M/D archive paths from "
                        "independent source/GWS verification manifests"
                    ),
                    "equivalence": (
                        "source_vs_s3 proves cloud-to-object parity; "
                        "source_vs_gws proves edge-source-to-GWS parity"
                    ),
                },
                "jobs": {},
            }
            for job in config["jobs"]:
                progress.update(
                    {
                        "updated_at": utc_now(),
                        "state": "running",
                        "current_job": job["name"],
                    }
                )
                write_progress(root, progress)
                patterns = COMMON_EXCLUDES + job.get("exclude", [])
                local = local_inventory(
                    job["source"], patterns, job.get("settle_age", "15m")
                )
                s3 = lister.inventory(job, local)
                # The source is live while a full remote listing is built.
                # Exclude anything that changed during that window so a newer
                # object cannot be misreported as a destructive mismatch.
                local = retain_unchanged_local_snapshot(job["source"], local)
                gws = gws_inventory(config, job)
                gws_source = mirror_manifest_inventory(config, job, "source")
                write_tsv(stage / f"{job['name']}-local.tsv", local)
                write_tsv(stage / f"{job['name']}-s3.tsv", s3)
                if gws:
                    write_tsv(stage / f"{job['name']}-gws.tsv", gws)
                if gws_source:
                    write_tsv(
                        stage / f"{job['name']}-gws-source.tsv", gws_source
                    )
                report["jobs"][job["name"]] = {
                    "source": job["source"],
                    "gws": job.get("gws_destination", ""),
                    "s3": f"s3://{config['bucket']}/{job['destination'].strip('/')}",
                    "source_vs_s3": compare(local, s3),
                    "source_vs_gws": (
                        compare(gws_source, gws)
                        if gws_source or gws
                        else None
                    ),
                    # Raw data is intentionally reorganised on GWS, so a
                    # byte-path GWS/S3 comparison would manufacture gaps.
                    "gws_vs_s3": None,
                    "gws_evidence": (
                        "independent canonical stream manifests"
                        if gws_source or gws
                        else None
                    ),
                }
                progress["completed_jobs"].append(job["name"])
            (stage / "comparison.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
            (stage / "comparison.md").write_text(
                render_markdown(report), encoding="utf-8"
            )
            (stage / "catalog.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "generated_at": generated_at,
                        "streams": config["streams"],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            progress.update(
                {
                    "updated_at": utc_now(),
                    "state": "publishing",
                    "current_job": None,
                }
            )
            write_progress(root, progress)
            publish(root, stage, generated_at)

        subprocess.run(
            [
                "/usr/bin/rclone",
                "copy",
                str(root / "latest"),
                f"--config={config['rclone_config']}",
                f"{config['remote']}:{config['bucket']}/data/internal/"
                "aurora-cloud/manifests/object-store/latest",
            ],
            check=True,
        )
    except Exception as error:
        progress.update(
            {
                "updated_at": utc_now(),
                "state": "failed",
                "error": f"{type(error).__name__}: {error}"[:1000],
            }
        )
        write_progress(root, progress)
        raise
    progress.update(
        {
            "updated_at": utc_now(),
            "state": "complete",
            "current_job": None,
            "report_generated_at": generated_at,
        }
    )
    write_progress(root, progress)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
