#!/usr/bin/env python3
"""Newest-first, receipt-backed delivery to both Aurora archives.

Source-sync jobs enqueue the exact files that they have safely landed in the
cloud raw mirror.  A short systemd worker then sends those paths to the JASMIN
GWS and object store without rescanning multi-terabyte trees.  The existing
full inventories remain the strict independent audit; this queue is the fast
delivery lane and its receipts are intentionally not pruning evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from typing import Iterable, Sequence


CONFIG_PATH = Path(
    os.environ.get(
        "AURORA_ARCHIVE_DISPATCH_CONFIG",
        "/etc/aurora-archive-dispatch/config.json",
    )
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def duration_seconds(value: str | int | float) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    text = str(value).strip().lower()
    return int(text[:-1]) * units[text[-1]]


def load_config(path: Path = CONFIG_PATH) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("archive-dispatch config must be an object")
    return value


def connect(config: dict) -> sqlite3.Connection:
    database = Path(config["database"])
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS delivery (
            job TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime INTEGER NOT NULL,
            discovered_at TEXT NOT NULL,
            gws_delivered INTEGER NOT NULL DEFAULT 0,
            object_delivered INTEGER NOT NULL DEFAULT 0,
            gws_delivered_at TEXT,
            object_delivered_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (job, relative_path)
        );
        CREATE INDEX IF NOT EXISTS delivery_pending_priority
        ON delivery (mtime DESC)
        WHERE gws_delivered = 0 OR object_delivered = 0;
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    return connection


def _metadata_set(connection: sqlite3.Connection, key: str, value: object) -> None:
    connection.execute(
        """
        INSERT INTO metadata (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["key"]): str(row["value"])
        for row in connection.execute("SELECT key, value FROM metadata")
    }


def _job(config: dict, name: str) -> dict:
    jobs = config.get("jobs")
    if not isinstance(jobs, dict) or name not in jobs:
        raise ValueError(f"unknown archive-dispatch job: {name}")
    job = jobs[name]
    if not isinstance(job, dict):
        raise ValueError(f"invalid archive-dispatch job: {name}")
    return job


def excluded(path: str, patterns: Sequence[str]) -> bool:
    value = path.lstrip("/")
    for pattern in patterns:
        normalized = str(pattern).lstrip("/")
        variants = {normalized, normalized.replace("**/", "*/")}
        if normalized.startswith("**/"):
            variants.add(normalized[3:])
        if any(fnmatch.fnmatch(value, variant) for variant in variants):
            return True
    return False


def _safe_relative(root: Path, base: Path, value: str) -> tuple[str, Path]:
    candidate = (base / value.lstrip("/")).resolve(strict=False)
    root = root.resolve(strict=True)
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError(f"path escapes archive source root: {value}") from error
    if not relative or relative == "." or "\n" in relative or "\x00" in relative:
        raise ValueError(f"unsupported archive path: {value!r}")
    return relative, candidate


def _paths_from_file(path: Path, *, null: bool, records_format: str) -> list[str]:
    raw = path.read_bytes()
    records = raw.split(b"\0") if null else raw.splitlines()
    result: list[str] = []
    for record in records:
        if not record:
            continue
        text = os.fsdecode(record)
        if records_format == "radar":
            fields = text.split("\t", 2)
            if len(fields) != 3:
                raise ValueError(f"invalid radar queue record: {text!r}")
            text = fields[2]
        elif records_format == "rsync":
            fields = text.split("\t", 1)
            if len(fields) != 2 or not fields[0].startswith(">f"):
                continue
            text = fields[1]
        result.append(text.removeprefix("./"))
    return result


def enqueue_paths(
    config: dict,
    connection: sqlite3.Connection,
    *,
    job_name: str,
    base: Path,
    paths: Iterable[str],
) -> dict[str, int]:
    job = _job(config, job_name)
    root = Path(job["source"])
    patterns = [str(value) for value in job.get("exclude", [])]
    discovered_at = utc_now()
    inserted = 0
    unchanged = 0
    missing = 0
    ignored = 0
    seen: set[str] = set()
    with connection:
        for value in paths:
            relative, candidate = _safe_relative(root, base, value)
            if relative in seen:
                continue
            seen.add(relative)
            if excluded(relative, patterns):
                ignored += 1
                continue
            try:
                stat = candidate.stat()
            except FileNotFoundError:
                missing += 1
                continue
            if not candidate.is_file():
                ignored += 1
                continue
            previous = connection.execute(
                "SELECT size, mtime FROM delivery WHERE job = ? AND relative_path = ?",
                (job_name, relative),
            ).fetchone()
            size = int(stat.st_size)
            mtime = int(stat.st_mtime)
            if previous and int(previous["size"]) == size and int(previous["mtime"]) == mtime:
                unchanged += 1
            else:
                inserted += 1
            connection.execute(
                """
                INSERT INTO delivery (
                    job, relative_path, size, mtime, discovered_at,
                    gws_delivered, object_delivered, attempts, last_error
                ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, '')
                ON CONFLICT(job, relative_path) DO UPDATE SET
                    size = excluded.size,
                    mtime = excluded.mtime,
                    discovered_at = excluded.discovered_at,
                    gws_delivered = CASE
                        WHEN delivery.size != excluded.size OR delivery.mtime != excluded.mtime
                        THEN 0 ELSE delivery.gws_delivered END,
                    object_delivered = CASE
                        WHEN delivery.size != excluded.size OR delivery.mtime != excluded.mtime
                        THEN 0 ELSE delivery.object_delivered END,
                    gws_delivered_at = CASE
                        WHEN delivery.size != excluded.size OR delivery.mtime != excluded.mtime
                        THEN NULL ELSE delivery.gws_delivered_at END,
                    object_delivered_at = CASE
                        WHEN delivery.size != excluded.size OR delivery.mtime != excluded.mtime
                        THEN NULL ELSE delivery.object_delivered_at END,
                    last_error = CASE
                        WHEN delivery.size != excluded.size OR delivery.mtime != excluded.mtime
                        THEN '' ELSE delivery.last_error END
                """,
                (job_name, relative, size, mtime, discovered_at),
            )
        _metadata_set(connection, "last_enqueue_at", discovered_at)
        _metadata_set(connection, "last_enqueue_job", job_name)
        _metadata_set(connection, "last_enqueue_count", inserted)
    return {
        "inserted": inserted,
        "unchanged": unchanged,
        "missing": missing,
        "ignored": ignored,
    }


def scan_job(
    config: dict,
    connection: sqlite3.Connection,
    *,
    job_name: str,
    lookback_hours: float,
) -> dict[str, int]:
    job = _job(config, job_name)
    root = Path(job["source"])
    cutoff = time.time() - max(lookback_hours, 0.0) * 3600
    paths: list[str] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in {".git", ".venv", "__pycache__", ".cache"}
            and not name.endswith((".partial", ".tmp"))
        ]
        for name in filenames:
            path = Path(directory, name)
            try:
                if path.stat().st_mtime >= cutoff:
                    paths.append(path.relative_to(root).as_posix())
            except FileNotFoundError:
                continue
    return enqueue_paths(
        config,
        connection,
        job_name=job_name,
        base=root,
        paths=paths,
    )


def _select_batch(
    config: dict,
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    limits = config.get("limits", {})
    max_files = int(limits.get("max_files_per_run", 5000))
    max_bytes = int(limits.get("max_bytes_per_run", 20 * 1024**3))
    candidates = connection.execute(
        """
        SELECT * FROM delivery
        WHERE gws_delivered = 0 OR object_delivered = 0
        ORDER BY mtime DESC, discovered_at DESC
        LIMIT ?
        """,
        (max(max_files * 4, max_files),),
    ).fetchall()
    selected: list[sqlite3.Row] = []
    selected_bytes = 0
    now_epoch = time.time()
    for row in candidates:
        job = _job(config, str(row["job"]))
        settle_before = now_epoch - duration_seconds(job.get("settle_age", "15m"))
        if int(row["mtime"]) > settle_before:
            continue
        size = int(row["size"])
        if selected and (len(selected) >= max_files or selected_bytes + size > max_bytes):
            break
        selected.append(row)
        selected_bytes += size
    return selected


def _write_file_lists(directory: Path, rows: Sequence[sqlite3.Row]) -> tuple[Path, Path]:
    line_path = directory / "files.txt"
    nul_path = directory / "files.nul"
    values = [str(row["relative_path"]) for row in rows]
    line_path.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")
    nul_path.write_bytes(b"\0".join(os.fsencode(value) for value in values) + (b"\0" if values else b""))
    return line_path, nul_path


def _ssh_command(config: dict, host: str) -> str:
    pieces = [
        "ssh",
        "-i", str(config["gws_key"]),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "Compression=yes",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    known_hosts = config.get("gws_known_hosts")
    if known_hosts:
        pieces.extend(["-o", f"UserKnownHostsFile={known_hosts}"])
    # Paths are deployment-controlled and contain no shell metacharacters.
    return " ".join(pieces)


def deliver_gws(config: dict, job: dict, rows: Sequence[sqlite3.Row], nul_path: Path) -> None:
    failures: list[str] = []
    for host in config.get("gws_hosts", []):
        command = [
            "/usr/bin/rsync",
            "-a",
            "--partial",
            "--delay-updates",
            "--timeout=120",
            "--from0",
            f"--files-from={nul_path}",
            "-e",
            _ssh_command(config, str(host)),
            f"{str(job['source']).rstrip('/')}/",
            f"{config['gws_user']}@{host}:{str(job['gws_destination']).rstrip('/')}/",
        ]
        completed = subprocess.run(command, text=True, capture_output=True)
        if completed.returncode == 0:
            return
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        failures.append(f"{host}: {detail[-1] if detail else completed.returncode}")
    raise RuntimeError("all GWS delivery hosts failed: " + "; ".join(failures))


def deliver_object(config: dict, job: dict, rows: Sequence[sqlite3.Row], line_path: Path) -> None:
    destination = (
        f"{config['object_remote']}:{config['object_bucket']}/"
        f"{str(job['object_destination']).strip('/')}"
    )
    command = [
        "/usr/bin/rclone",
        "copy",
        f"{str(job['source']).rstrip('/')}/",
        destination,
        f"--config={config['rclone_config']}",
        f"--files-from-raw={line_path}",
        "--no-traverse",
        "--checkers=8",
        f"--transfers={int(job.get('transfers', 8))}",
        "--s3-upload-concurrency=4",
        "--s3-chunk-size=64M",
        "--contimeout=30s",
        "--timeout=10m",
        "--retries=6",
        "--low-level-retries=20",
        "--use-server-modtime",
    ]
    subprocess.run(command, check=True)


def _rows_still_unchanged(config: dict, rows: Sequence[sqlite3.Row]) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    stable: list[sqlite3.Row] = []
    changed: list[sqlite3.Row] = []
    for row in rows:
        job = _job(config, str(row["job"]))
        path = Path(job["source"]) / str(row["relative_path"])
        try:
            stat = path.stat()
        except FileNotFoundError:
            changed.append(row)
            continue
        if int(stat.st_size) == int(row["size"]) and int(stat.st_mtime) == int(row["mtime"]):
            stable.append(row)
        else:
            changed.append(row)
    return stable, changed


def _mark_changed(config: dict, connection: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> None:
    with connection:
        for row in rows:
            job = _job(config, str(row["job"]))
            path = Path(job["source"]) / str(row["relative_path"])
            try:
                stat = path.stat()
            except FileNotFoundError:
                connection.execute(
                    "UPDATE delivery SET last_error = ? WHERE job = ? AND relative_path = ?",
                    ("local file disappeared before delivery", row["job"], row["relative_path"]),
                )
                continue
            connection.execute(
                """
                UPDATE delivery SET size = ?, mtime = ?, gws_delivered = 0,
                    object_delivered = 0, gws_delivered_at = NULL,
                    object_delivered_at = NULL,
                    last_error = 'local file changed during delivery'
                WHERE job = ? AND relative_path = ?
                """,
                (int(stat.st_size), int(stat.st_mtime), row["job"], row["relative_path"]),
            )


def _mark_destination(
    connection: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
    destination: str,
    stamp: str,
) -> int:
    column = "gws_delivered" if destination == "gws" else "object_delivered"
    timestamp_column = f"{destination}_delivered_at"
    updated = 0
    with connection:
        for row in rows:
            cursor = connection.execute(
                f"""
                UPDATE delivery SET {column} = 1, {timestamp_column} = ?,
                    last_error = ''
                WHERE job = ? AND relative_path = ? AND size = ? AND mtime = ?
                """,
                (
                    stamp,
                    row["job"],
                    row["relative_path"],
                    row["size"],
                    row["mtime"],
                ),
            )
            updated += max(cursor.rowcount, 0)
    return updated


def _mark_failure(connection: sqlite3.Connection, rows: Sequence[sqlite3.Row], error: str) -> None:
    with connection:
        connection.executemany(
            """
            UPDATE delivery SET attempts = attempts + 1, last_error = ?
            WHERE job = ? AND relative_path = ? AND size = ? AND mtime = ?
            """,
            [
                (
                    error[:1000],
                    row["job"],
                    row["relative_path"],
                    row["size"],
                    row["mtime"],
                )
                for row in rows
            ],
        )


def build_status(config: dict, connection: sqlite3.Connection) -> dict:
    now_epoch = time.time()
    aggregate = connection.execute(
        """
        SELECT
          COUNT(*) AS tracked,
          SUM(CASE WHEN gws_delivered = 0 OR object_delivered = 0 THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN gws_delivered = 0 THEN 1 ELSE 0 END) AS gws_pending,
          SUM(CASE WHEN object_delivered = 0 THEN 1 ELSE 0 END) AS object_pending,
          SUM(CASE WHEN gws_delivered = 0 OR object_delivered = 0 THEN size ELSE 0 END) AS pending_bytes,
          MIN(CASE WHEN gws_delivered = 0 OR object_delivered = 0 THEN mtime END) AS oldest_pending,
          MAX(CASE WHEN gws_delivered = 0 OR object_delivered = 0 THEN mtime END) AS newest_pending,
          SUM(CASE WHEN gws_delivered = 1 AND object_delivered = 1 THEN 1 ELSE 0 END) AS delivered
        FROM delivery
        """
    ).fetchone()
    metadata = _metadata(connection)
    oldest = aggregate["oldest_pending"]
    newest = aggregate["newest_pending"]
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "queue": {
            "tracked_files": int(aggregate["tracked"] or 0),
            "dual_delivered_files": int(aggregate["delivered"] or 0),
            "pending_files": int(aggregate["pending"] or 0),
            "pending_bytes": int(aggregate["pending_bytes"] or 0),
            "gws_pending_files": int(aggregate["gws_pending"] or 0),
            "object_store_pending_files": int(aggregate["object_pending"] or 0),
            "oldest_pending_age_minutes": (
                max(now_epoch - float(oldest), 0.0) / 60 if oldest is not None else 0.0
            ),
            "newest_pending_age_minutes": (
                max(now_epoch - float(newest), 0.0) / 60 if newest is not None else 0.0
            ),
        },
        "last_run": {
            "state": metadata.get("last_run_state", "never"),
            "started_at": metadata.get("last_run_started_at"),
            "finished_at": metadata.get("last_run_finished_at"),
            "selected_files": int(metadata.get("last_run_selected_files", "0")),
            "selected_bytes": int(metadata.get("last_run_selected_bytes", "0")),
            "gws_delivered_files": int(metadata.get("last_run_gws_files", "0")),
            "object_store_delivered_files": int(metadata.get("last_run_object_files", "0")),
            "error": metadata.get("last_run_error", ""),
        },
        "last_success_at": metadata.get("last_success_at"),
        "last_enqueue_at": metadata.get("last_enqueue_at"),
    }


def write_status(config: dict, connection: sqlite3.Connection) -> dict:
    status = build_status(config, connection)
    path = Path(config["status_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return status


def _write_receipt(config: dict, receipt: dict) -> None:
    root = Path(config["receipt_root"])
    root.mkdir(parents=True, exist_ok=True)
    stamp = receipt["started_at"].replace(":", "").replace("-", "")
    path = root / f"{stamp}.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    keep = max(24, int(config.get("receipt_history_keep", 336)))
    receipts = sorted(root.glob("*.json"))
    for obsolete in receipts[:-keep]:
        obsolete.unlink(missing_ok=True)


def run_worker(config: dict, connection: sqlite3.Connection) -> int:
    lock_path = Path(config["lock_path"])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        rows = _select_batch(config, connection)
        started_at = utc_now()
        selected_bytes = sum(int(row["size"]) for row in rows)
        with connection:
            _metadata_set(connection, "last_run_state", "running")
            _metadata_set(connection, "last_run_started_at", started_at)
            _metadata_set(connection, "last_run_selected_files", len(rows))
            _metadata_set(connection, "last_run_selected_bytes", selected_bytes)
            _metadata_set(connection, "last_run_error", "")
        if not rows:
            finished_at = utc_now()
            with connection:
                _metadata_set(connection, "last_run_state", "idle")
                _metadata_set(connection, "last_run_finished_at", finished_at)
                _metadata_set(connection, "last_success_at", finished_at)
                _metadata_set(connection, "last_run_gws_files", 0)
                _metadata_set(connection, "last_run_object_files", 0)
            write_status(config, connection)
            return 0

        receipt = {
            "schema_version": 1,
            "started_at": started_at,
            "finished_at": None,
            "state": "running",
            "selected_files": len(rows),
            "selected_bytes": selected_bytes,
            "newest_first": True,
            "destinations": {},
        }
        failures: list[str] = []
        gws_count = 0
        object_count = 0
        with tempfile.TemporaryDirectory(prefix="archive-dispatch-") as temporary:
            directory = Path(temporary)
            for job_name in dict.fromkeys(str(row["job"]) for row in rows):
                job = _job(config, job_name)
                job_rows = [row for row in rows if row["job"] == job_name]
                stable, changed = _rows_still_unchanged(config, job_rows)
                _mark_changed(config, connection, changed)
                if not stable:
                    continue
                gws_rows = [row for row in stable if not int(row["gws_delivered"])]
                object_rows = [row for row in stable if not int(row["object_delivered"])]
                job_dir = directory / job_name
                job_dir.mkdir()
                if gws_rows:
                    gws_dir = job_dir / "gws-files"
                    gws_dir.mkdir()
                    _unused, nul = _write_file_lists(gws_dir, gws_rows)
                    try:
                        deliver_gws(config, job, gws_rows, nul)
                        stamp = utc_now()
                        still_stable, changed_after = _rows_still_unchanged(config, gws_rows)
                        _mark_changed(config, connection, changed_after)
                        gws_count += _mark_destination(
                            connection, still_stable, "gws", stamp
                        )
                    except Exception as error:
                        detail = f"GWS {job_name}: {type(error).__name__}: {error}"
                        failures.append(detail)
                        _mark_failure(connection, gws_rows, detail)
                if object_rows:
                    object_rows, changed_before = _rows_still_unchanged(
                        config, object_rows
                    )
                    _mark_changed(config, connection, changed_before)
                if object_rows:
                    object_dir = job_dir / "object-files"
                    object_dir.mkdir()
                    line, _unused = _write_file_lists(object_dir, object_rows)
                    try:
                        deliver_object(config, job, object_rows, line)
                        stamp = utc_now()
                        still_stable, changed_after = _rows_still_unchanged(config, object_rows)
                        _mark_changed(config, connection, changed_after)
                        object_count += _mark_destination(
                            connection, still_stable, "object", stamp
                        )
                    except Exception as error:
                        detail = f"object store {job_name}: {type(error).__name__}: {error}"
                        failures.append(detail)
                        _mark_failure(connection, object_rows, detail)

        finished_at = utc_now()
        state = "failed" if failures else "complete"
        error_text = "; ".join(failures)[:2000]
        with connection:
            _metadata_set(connection, "last_run_state", state)
            _metadata_set(connection, "last_run_finished_at", finished_at)
            _metadata_set(connection, "last_run_gws_files", gws_count)
            _metadata_set(connection, "last_run_object_files", object_count)
            _metadata_set(connection, "last_run_error", error_text)
            if not failures:
                _metadata_set(connection, "last_success_at", finished_at)
            retention_days = max(1, int(config.get("completed_queue_retention_days", 14)))
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)).isoformat()
            connection.execute(
                """
                DELETE FROM delivery
                WHERE gws_delivered = 1 AND object_delivered = 1
                  AND COALESCE(object_delivered_at, gws_delivered_at, discovered_at) < ?
                """,
                (cutoff,),
            )
        receipt.update(
            {
                "finished_at": finished_at,
                "state": state,
                "destinations": {
                    "gws": {"delivered_files": gws_count},
                    "object_store": {"delivered_files": object_count},
                },
                "error": error_text,
            }
        )
        _write_receipt(config, receipt)
        write_status(config, connection)
        return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue", help="Queue exact landed paths")
    enqueue.add_argument("--job", required=True)
    enqueue.add_argument("--base", type=Path, required=True)
    enqueue.add_argument("--files-from", type=Path, required=True)
    enqueue.add_argument("--null", action="store_true")
    enqueue.add_argument(
        "--records-format",
        choices=("path", "radar", "rsync"),
        default="path",
    )

    scan = subparsers.add_parser("scan", help="Seed recent files after deployment")
    scan.add_argument("--job", required=True)
    scan.add_argument("--lookback-hours", type=float, default=48.0)

    subparsers.add_parser("run", help="Deliver one bounded newest-first batch")
    subparsers.add_parser("status", help="Refresh and print queue status")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    connection = connect(config)
    try:
        if args.command == "enqueue":
            paths = _paths_from_file(
                args.files_from,
                null=args.null,
                records_format=args.records_format,
            )
            result = enqueue_paths(
                config,
                connection,
                job_name=args.job,
                base=args.base,
                paths=paths,
            )
            status = write_status(config, connection)
            print(json.dumps({"enqueue": result, "queue": status["queue"]}))
            return 0
        if args.command == "scan":
            result = scan_job(
                config,
                connection,
                job_name=args.job,
                lookback_hours=args.lookback_hours,
            )
            status = write_status(config, connection)
            print(json.dumps({"scan": result, "queue": status["queue"]}))
            return 0
        if args.command == "run":
            return run_worker(config, connection)
        status = write_status(config, connection)
        print(json.dumps(status, indent=2))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
