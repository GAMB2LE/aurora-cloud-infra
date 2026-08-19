#!/usr/bin/env python3
"""Build reproducible cloud/GWS/S3 comparison evidence from the catalogue."""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import as_completed, ThreadPoolExecutor
import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import random
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--job",
        action="append",
        help=(
            "Refresh only this catalogue job; may be repeated. Requires "
            "--reuse-latest so unaffected jobs retain their last full "
            "evidence."
        ),
    )
    parser.add_argument(
        "--reuse-latest",
        action="store_true",
        help=(
            "Publish a complete incremental report using the latest report "
            "as its base."
        ),
    )
    return parser.parse_args()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def duration_seconds(value: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(value[:-1]) * units[value[-1].lower()]


def verification_settle_age(job: dict) -> str:
    return job.get(
        "verification_settle_age",
        job.get("settle_age", "15m"),
    )


def credential_path(config: dict, key: str, credential_name: str) -> str:
    """Prefer a systemd credential over a path inside a private runtime dir."""
    credential_directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if credential_directory:
        credential = Path(credential_directory, credential_name)
        if credential.is_file():
            return str(credential)
    return str(config.get(key, ""))


def excluded(path: str, patterns: list[str]) -> bool:
    value = path.lstrip("/")
    for pattern in patterns:
        normalized = pattern.lstrip("/")
        variants = {
            normalized,
            normalized.replace("**/", "*/"),
        }
        if normalized.startswith("**/"):
            variants.add(normalized[3:])
        if any(fnmatch.fnmatch(value, variant) for variant in variants):
            return True
    return False


def local_inventory(
    root: str,
    patterns: list[str],
    settle_age: str,
    copy_links: bool = False,
) -> dict[str, dict]:
    result: dict[str, dict] = {}
    base = Path(root)
    if not base.exists():
        return result
    settled_before = time.time() - duration_seconds(settle_age)
    for directory, dirnames, filenames in os.walk(base):
        directory_path = Path(directory)
        try:
            directory_relative = directory_path.relative_to(base).as_posix()
        except ValueError:
            directory_relative = ""

        def child_excluded(name: str) -> bool:
            relative = (
                f"{directory_relative}/{name}"
                if directory_relative not in {"", "."}
                else name
            )
            # A pattern such as ``wxcam.zarr/**`` describes the descendants,
            # not the directory token itself. Probe one synthetic child so
            # os.walk never enters a tree whose every file is excluded.
            return excluded(relative, patterns) or excluded(
                f"{relative.rstrip('/')}/__inventory_probe__",
                patterns,
            )

        dirnames[:] = [
            name
            for name in dirnames
            if name not in {".git", ".venv", "__pycache__", ".cache"}
            and not name.endswith((".partial", ".tmp"))
            and not child_excluded(name)
        ]
        for name in filenames:
            path = Path(directory, name)
            try:
                # Operational pointers are excluded by default. Jobs that
                # explicitly archive their targets verify the dereferenced
                # bytes under the symlink's relative path.
                if path.is_symlink() and not copy_links:
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
    root: str,
    rows: dict[str, dict],
    copy_links: bool = False,
) -> dict[str, dict]:
    """Keep only files unchanged for the entire remote inventory window."""
    base = Path(root)
    stable: dict[str, dict] = {}
    for relative, row in rows.items():
        path = base / relative
        try:
            if path.is_symlink() and not copy_links:
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
        attempts = max(1, int(self.config.get("s3_list_attempts", 3)))
        retry_delay = max(
            0, int(self.config.get("s3_list_retry_delay_seconds", 30))
        )
        retry_max_delay = max(
            retry_delay,
            int(self.config.get("s3_list_retry_max_delay_seconds", 300)),
        )
        for attempt in range(1, attempts + 1):
            try:
                with self.list_slots:
                    completed = subprocess.run(
                        command,
                        check=True,
                        text=True,
                        stdout=subprocess.PIPE,
                        timeout=int(
                            self.config.get("list_timeout_seconds", 1800)
                        ),
                    )
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt == attempts:
                    raise
                # A gateway timeout is usually shared service pressure, not a
                # bad path.  Back off exponentially and add jitter so all
                # concurrent shard scans do not retry as one thundering herd.
                delay = min(
                    retry_max_delay,
                    retry_delay * (2 ** (attempt - 1)),
                )
                time.sleep(delay + random.uniform(0, min(15, delay / 4)))
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

        local_prefixes = {path.split("/", 1)[0] for path in local if "/" in path}
        sharded = set(job.get("sharded_prefixes", []))
        shard_all_prefixes = bool(job.get("shard_all_prefixes", False))
        local_root_files = {path for path in local if "/" not in path}
        shards_fully_describe_tree = (
            bool(local_prefixes)
            and not local_root_files
            and (
                (bool(sharded) and local_prefixes <= sharded)
                # When every source path is below a first-level prefix, use
                # those source-derived prefixes directly.  This avoids a
                # pathological top-level S3 listing for large live-product
                # trees while retaining authoritative source coverage.
                or shard_all_prefixes
            )
        )
        if shards_fully_describe_tree:
            # Some S3 gateways time out listing a very large logical root even
            # though its configured prefixes list reliably. When every local
            # path is beneath an explicitly configured shard and there are no
            # root-level files to reconcile, the root listing adds no source
            # coverage and can be skipped safely.
            top_level: list[dict] = []
            remote_prefixes: set[str] = set()
        else:
            top_level = self.list_json(
                remote, recursive=False, files_only=False
            )
            remote_prefixes = {
                item["Path"]
                for item in top_level
                if item.get("IsDir") and not tree_excluded(item["Path"])
            }
        prefixes = sorted(local_prefixes | remote_prefixes)

        def list_shallow_shards(prefix: str) -> tuple[str, list[dict]]:
            prefix_remote = f"{remote}/{prefix}"
            if prefix not in sharded and not shard_all_prefixes:
                return prefix, self.list_json(prefix_remote)

            # Flat instrument families (notably CL61) can contain tens of
            # thousands of files directly beneath one prefix.  A delimiter-
            # based shallow S3 listing is pathologically slow on the JASMIN
            # gateway for this shape.  The settled local inventory already
            # proves that this family is flat, so use the ordinary recursive
            # files-only ListObjects path.  It covers the same source paths
            # without the expensive directory emulation.
            local_relatives = [
                path[len(prefix) + 1 :]
                for path in local
                if path.startswith(f"{prefix}/")
            ]
            if local_relatives and all("/" not in path for path in local_relatives):
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
    """Return an independent canonical or direct GWS inventory."""
    if job["name"] == "raw":
        return mirror_manifest_inventory(config, job, "gws")
    destination = job.get("gws_destination")
    if not destination:
        return {}
    patterns = COMMON_EXCLUDES + job.get("exclude", [])
    prune_roots = []
    for pattern in patterns:
        normalized = str(pattern).lstrip("/")
        if normalized.endswith("/**"):
            root = normalized[:-3].rstrip("/")
            if root and not any(token in root for token in "*?["):
                prune_roots.append(root)
    find_bits = ["find", "."]
    if prune_roots:
        find_bits.extend(["("])
        for index, root in enumerate(sorted(set(prune_roots))):
            if index:
                find_bits.append("-o")
            find_bits.extend(["-path", f"./{root}"])
        find_bits.extend([")", "-prune", "-o"])
    find_bits.extend(
        [
            "-type",
            "f",
            "-printf",
            r"%P\t%s\t%Ts\n",
        ]
    )
    remote_command = (
        f"cd {json.dumps(destination)} && "
        + " ".join(json.dumps(bit) for bit in find_bits)
    )
    ssh_base = [
        "ssh",
        "-i",
        credential_path(config, "gws_key", "gws-key"),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "Compression=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    known_hosts = credential_path(
        config,
        "gws_known_hosts",
        "gws-known-hosts",
    )
    if known_hosts:
        ssh_base.extend(["-o", f"UserKnownHostsFile={known_hosts}"])
    failures: list[str] = []
    hosts = config.get("gws_hosts", [])
    attempts = max(1, int(config.get("gws_inventory_attempts", 3)))
    retry_delay = max(0, int(config.get("gws_inventory_retry_delay_seconds", 15)))
    for attempt in range(1, attempts + 1):
        for host in hosts:
            target = f"{config['gws_user']}@{host}"
            try:
                completed = subprocess.run(
                    [*ssh_base, target, remote_command],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=7200,
                )
            except (OSError, subprocess.SubprocessError) as error:
                stderr = getattr(error, "stderr", "") or ""
                detail = stderr.strip().splitlines()[-1:] or [str(error)]
                failures.append(
                    f"attempt {attempt}/{attempts} {host}: {detail[0]}"
                )
                continue
            result: dict[str, dict] = {}
            for line in completed.stdout.splitlines():
                if not line:
                    continue
                relative, size, mtime = line.split("\t")
                if excluded(relative, patterns):
                    continue
                result[relative] = {
                    "relative_path": relative,
                    "size": int(size),
                    "mtime": int(float(mtime)),
                    "checksum": "",
                }
            return result
        if attempt < attempts:
            time.sleep(retry_delay * attempt)
    raise RuntimeError(
        f"all GWS inventory hosts failed for {job['name']}: "
        + "; ".join(failures)
    )


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
        "| Job | Source | S3 | Missing | Pending upload | Size mismatch |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, values in report["jobs"].items():
        check = values["source_vs_s3"]
        pending = values.get("pending_upload", {})
        lines.append(
            f"| {name} | {check['left_count']} | {check['right_count']} | "
            f"{len(check['missing_from_right'])} | "
            f"{len(pending.get('missing_from_right', []))} | "
            f"{len(check['size_mismatch'])} |"
        )
    return "\n".join(lines) + "\n"


def publish(
    root: Path,
    stage: Path,
    generated_at: str,
    history_keep: int,
) -> None:
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
    snapshots = sorted(
        path for path in history.parent.iterdir() if path.is_dir()
    )
    for obsolete in snapshots[: -max(2, history_keep)]:
        shutil.rmtree(obsolete)


def inventory_job(
    config: dict,
    lister: S3Lister,
    job: dict,
    stage: Path,
    update_phase,
) -> dict:
    """Inventory one independent archive family.

    Each family owns distinct output files, so multiple jobs can run in
    parallel while the shared S3 lister enforces the configured process cap.
    """
    name = job["name"]
    update_phase(name, "local_inventory")
    patterns = COMMON_EXCLUDES + job.get("exclude", [])
    # Settled products are archive evidence. Recent outputs stay observable as
    # pending uploads without resetting the stable-parity gate.
    local_live = local_inventory(
        job["source"],
        patterns,
        "0s",
        bool(job.get("copy_links")),
    )
    local = local_inventory(
        job["source"],
        patterns,
        verification_settle_age(job),
        bool(job.get("copy_links")),
    )
    pending_upload = {
        path: entry for path, entry in local_live.items() if path not in local
    }
    update_phase(name, "gws_inventory")
    gws = gws_inventory(config, job)
    # Prove the usually-cheaper GWS path before starting a large object-store
    # listing. A transient JASMIN outage must not waste that listing.
    update_phase(name, "object_store_inventory")
    s3 = lister.inventory(job, local)
    update_phase(name, "stability_check")
    local = retain_unchanged_local_snapshot(
        job["source"],
        local,
        bool(job.get("copy_links")),
    )
    gws_source = (
        mirror_manifest_inventory(config, job, "source")
        if name == "raw"
        else local
    )
    update_phase(name, "comparison")
    write_tsv(stage / f"{name}-local.tsv", local)
    write_tsv(stage / f"{name}-s3.tsv", s3)
    if gws:
        write_tsv(stage / f"{name}-gws.tsv", gws)
    if gws_source:
        write_tsv(stage / f"{name}-gws-source.tsv", gws_source)
    return {
        "source": job["source"],
        "gws": job.get("gws_destination", ""),
        "s3": f"s3://{config['bucket']}/{job['destination'].strip('/')}",
        "source_vs_s3": compare(local, s3),
        "pending_upload": compare(pending_upload, s3),
        "verification_settle_age": verification_settle_age(job),
        "source_vs_gws": (
            compare(gws_source, gws) if gws_source or gws else None
        ),
        # Raw data is intentionally reorganised on GWS, so a byte-path GWS/S3
        # comparison would manufacture gaps.
        "gws_vs_s3": None,
        "gws_evidence": (
            (
                "independent canonical stream manifests"
                if name == "raw"
                else "direct remote GWS inventory"
            )
            if gws_source or gws
            else None
        ),
        # A partial report may reuse other families, so every family carries
        # its own proof timestamp.  This is the completion time of a complete
        # source/GWS/object-store inventory for this family, not the age of the
        # report used as a merge base.
        "verified_at": utc_now(),
        "verification_scope": "full_family",
    }


def load_incremental_base(
    root: Path,
    config: dict,
    selected_names: set[str],
) -> tuple[dict, str]:
    """Load a structurally complete report as a merge base.

    Age and chain depth do not make a merge unsafe: reused families retain
    their own old ``verified_at`` timestamps and the verification gate keeps
    those domains stale.  Rejecting an old base here previously prevented a
    complete raw-family recheck from refreshing strict retention evidence
    after an unrelated product audit failed.
    """
    report_path = root / "latest" / "comparison.json"
    raw = report_path.read_bytes()
    report = json.loads(raw)
    configured_names = {job["name"] for job in config["jobs"]}
    report_names = set(report.get("jobs", {}))
    if report_names != configured_names:
        raise RuntimeError(
            "latest report jobs do not match the current catalogue: "
            f"report={sorted(report_names)} catalogue={sorted(configured_names)}"
        )
    if not selected_names:
        raise RuntimeError("incremental inventory requires at least one --job")
    legacy_verified_at = report.get(
        "evidence_floor_generated_at",
        report["generated_at"],
    )
    for values in report["jobs"].values():
        values.setdefault("verified_at", legacy_verified_at)
        values.setdefault("verification_scope", "full_family")
    return report, hashlib.sha256(raw).hexdigest()


def write_report_files(stage: Path, report: dict, config: dict) -> None:
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
                "generated_at": report["generated_at"],
                "streams": config["streams"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def publish_job_checkpoint(
    *,
    root: Path,
    stage: Path,
    config: dict,
    base_report: dict,
    base_report_sha256: str,
    name: str,
    values: dict,
    verified_jobs: set[str] | None = None,
) -> tuple[dict, str]:
    """Publish one successful family without certifying unfinished families."""
    generated_at = utc_now()
    report = json.loads(json.dumps(base_report))
    report["jobs"][name] = values
    evidence_times = [
        item.get(
            "verified_at",
            report.get("evidence_floor_generated_at", report["generated_at"]),
        )
        for item in report["jobs"].values()
    ]
    report.update(
        {
            "schema_version": 5,
            "generated_at": generated_at,
            "verification_mode": "incremental",
            "verified_jobs": sorted(verified_jobs or {name}),
            "base_generated_at": base_report["generated_at"],
            "base_report_sha256": base_report_sha256,
            # Retained for older readers.  Policy v5 evaluates the per-family
            # timestamps instead of applying this oldest timestamp globally.
            "evidence_floor_generated_at": min(
                evidence_times,
                key=lambda value: dt.datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                ),
            ),
            "incremental_depth": int(base_report.get("incremental_depth", 0))
            + 1,
        }
    )
    with tempfile.TemporaryDirectory(
        prefix=".inventory-checkpoint-", dir=root
    ) as temporary:
        checkpoint = Path(temporary)
        shutil.copytree(root / "latest", checkpoint, dirs_exist_ok=True)
        for source in stage.glob(f"{name}-*.tsv"):
            shutil.copy2(source, checkpoint / source.name)
        write_report_files(checkpoint, report, config)
        publish(
            root,
            checkpoint,
            generated_at,
            int(config.get("history_keep", 12)),
        )
    raw = (root / "latest" / "comparison.json").read_bytes()
    return report, hashlib.sha256(raw).hexdigest()


def main() -> int:
    args = parse_args()
    config = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    generated_at = utc_now()
    root = Path(config["manifest_root"])
    root.mkdir(parents=True, exist_ok=True)
    lock_handle = (root / ".inventory.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise RuntimeError(
            "another object-store inventory is already running"
        ) from error
    configured_jobs = {job["name"]: job for job in config["jobs"]}
    selected_names = set(args.job or configured_jobs)
    unknown = selected_names - set(configured_jobs)
    if unknown:
        raise SystemExit(
            f"unknown catalogue jobs: {', '.join(sorted(unknown))}"
        )
    if args.reuse_latest and not args.job:
        raise SystemExit("--reuse-latest requires at least one --job")
    if (
        args.job
        and not args.reuse_latest
        and selected_names != set(configured_jobs)
    ):
        raise SystemExit("partial inventory requires --reuse-latest")
    selected_jobs = [
        job for job in config["jobs"] if job["name"] in selected_names
    ]
    base_report: dict | None = None
    base_report_sha256 = ""
    if args.reuse_latest:
        base_report, base_report_sha256 = load_incremental_base(
            root,
            config,
            selected_names,
        )
    checkpoint_report: dict | None = None
    checkpoint_report_sha256 = ""
    checkpoint_verified_jobs: set[str] = set()
    if base_report is not None and len(selected_jobs) > 1:
        # Multi-family incremental rechecks are resumable too.  A failed later
        # family must not discard an earlier complete family verification.
        checkpoint_report = json.loads(json.dumps(base_report))
        checkpoint_report_sha256 = base_report_sha256
    elif base_report is None and (root / "latest" / "comparison.json").exists():
        # A full audit still produces a genuinely full report only after every
        # family succeeds.  This merge base is used solely to checkpoint each
        # successful family as an explicitly incremental report while the
        # remaining families continue.
        try:
            checkpoint_report, checkpoint_report_sha256 = load_incremental_base(
                root,
                config,
                selected_names,
            )
        except (
            OSError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            # Checkpointing is an optimisation.  A missing or legacy-incomplete
            # latest report must not prevent a full audit from rebuilding the
            # canonical report from scratch.
            checkpoint_report = None
            checkpoint_report_sha256 = ""
    lister = S3Lister(config)
    progress = {
        "schema_version": 2,
        "run_generated_at": generated_at,
        "updated_at": generated_at,
        "state": "running",
        "verification_mode": (
            "incremental" if base_report is not None else "full"
        ),
        "verified_jobs": sorted(selected_names),
        "current_job": None,
        "phase": "starting",
        "completed_jobs": [],
        "total_jobs": len(selected_jobs),
        "jobs": {
            job["name"]: {
                "state": "pending",
                "phase": "pending",
                "updated_at": generated_at,
            }
            for job in selected_jobs
        },
    }
    progress_lock = threading.Lock()
    heartbeat_stop = threading.Event()

    def update_progress(**values: object) -> None:
        with progress_lock:
            progress.update(values)
            progress["updated_at"] = utc_now()
            write_progress(root, progress)

    def update_job(name: str, phase: str, *, state: str = "running", error: str = "") -> None:
        with progress_lock:
            job_progress = progress["jobs"][name]
            job_progress.update(
                {
                    "state": state,
                    "phase": phase,
                    "updated_at": utc_now(),
                }
            )
            if error:
                job_progress["error"] = error[:1000]
            running = sorted(
                job_name
                for job_name, value in progress["jobs"].items()
                if value["state"] == "running"
            )
            progress["current_jobs"] = running
            progress["current_job"] = running[0] if running else None
            progress["phase"] = (
                progress["jobs"][running[0]]["phase"] if running else phase
            )
            progress["completed_jobs"] = sorted(
                job_name
                for job_name, value in progress["jobs"].items()
                if value["state"] == "complete"
            )
            progress["updated_at"] = utc_now()
            write_progress(root, progress)

    def heartbeat() -> None:
        while not heartbeat_stop.wait(60):
            update_progress()

    update_progress()
    heartbeat_thread = threading.Thread(
        target=heartbeat,
        name="inventory-progress-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix=".inventory-", dir=root) as temporary:
            stage = Path(temporary)
            if base_report is not None:
                shutil.copytree(root / "latest", stage, dirs_exist_ok=True)
                report = json.loads(json.dumps(base_report))
                evidence_floor = report.get(
                    "evidence_floor_generated_at",
                    report["generated_at"],
                )
                report.update(
                    {
                        "schema_version": 5,
                        "generated_at": generated_at,
                        "verification_mode": "incremental",
                        "verified_jobs": sorted(selected_names),
                        "base_generated_at": base_report["generated_at"],
                        "base_report_sha256": base_report_sha256,
                        "evidence_floor_generated_at": evidence_floor,
                        "incremental_depth": int(
                            base_report.get("incremental_depth", 0)
                        )
                        + 1,
                    }
                )
            else:
                report = {
                    "schema_version": 5,
                    "generated_at": generated_at,
                    "verification_mode": "full",
                    "verified_jobs": [job["name"] for job in config["jobs"]],
                    "evidence_floor_generated_at": generated_at,
                    "incremental_depth": 0,
                    "layout_contract": {
                        "s3": "preserves settled cloud-ingress relative paths",
                        "gws_raw": (
                            "uses canonical per-stream Y/M/D archive paths from "
                            "independent source/GWS verification manifests"
                        ),
                        "gws_other": (
                            "preserves cloud relative paths and is inventoried "
                            "directly through a JASMIN transfer host"
                        ),
                        "equivalence": (
                            "source_vs_s3 proves cloud-to-object parity; "
                            "source_vs_gws proves edge-source-to-GWS parity"
                        ),
                    },
                    "jobs": {},
                }
            failures: list[str] = []
            workers = max(
                1,
                min(
                    int(config.get("job_workers", 3)),
                    len(selected_jobs),
                ),
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        inventory_job,
                        config,
                        lister,
                        job,
                        stage,
                        update_job,
                    ): job
                    for job in selected_jobs
                }
                for future in as_completed(futures):
                    job = futures[future]
                    name = job["name"]
                    try:
                        values = future.result()
                        report["jobs"][name] = values
                    except Exception as error:
                        detail = f"{type(error).__name__}: {error}"
                        failures.append(f"{name}: {detail}")
                        update_job(name, "failed", state="failed", error=detail)
                    else:
                        if checkpoint_report is not None:
                            checkpoint_verified_jobs.add(name)
                            (
                                checkpoint_report,
                                checkpoint_report_sha256,
                            ) = publish_job_checkpoint(
                                root=root,
                                stage=stage,
                                config=config,
                                base_report=checkpoint_report,
                                base_report_sha256=checkpoint_report_sha256,
                                name=name,
                                values=values,
                                verified_jobs=checkpoint_verified_jobs,
                            )
                        update_job(name, "complete", state="complete")
            if failures:
                raise RuntimeError("; ".join(failures))
            report["jobs"] = {
                job["name"]: report["jobs"][job["name"]]
                for job in config["jobs"]
            }
            report["generated_at"] = utc_now()
            report["evidence_floor_generated_at"] = min(
                (
                    values.get("verified_at", report["generated_at"])
                    for values in report["jobs"].values()
                ),
                key=lambda value: dt.datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                ),
            )
            write_report_files(stage, report, config)
            update_progress(
                state="publishing",
                current_job=None,
                phase="local_publish",
            )
            publish(
                root,
                stage,
                report["generated_at"],
                int(config.get("history_keep", 12)),
            )

        update_progress(phase="object_store_publish")
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
        update_progress(
            state="failed",
            phase="failed",
            error=f"{type(error).__name__}: {error}"[:1000],
        )
        raise
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
    update_progress(
        state="complete",
        current_job=None,
        phase="complete",
        report_generated_at=generated_at,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
