#!/usr/bin/env python3
"""Incrementally ingest immutable Menapia flight objects into AURORA raw storage."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Sequence


UTC = dt.timezone.utc
AUTH_FAILURE = re.compile(
    r"AccessDenied|InvalidAccessKeyId|SignatureDoesNotMatch|ExpiredToken|"
    r"RequestExpired|AuthFailure|credentials.*(?:invalid|expired)",
    re.IGNORECASE,
)
AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{12,}\b")
SECRET_VALUE = re.compile(r"\b[A-Za-z0-9/+_=.-]{32,}\b")
UNSUPPORTED_METADATA_FLAG = re.compile(
    r"(?:unknown flag|flag provided but not defined).*--?metadata", re.IGNORECASE
)
DATE_PATH = re.compile(
    r"(?:^|/)drone-uploads/(?P<year>\d{4})/(?P<month>\d{2})/"
    r"(?P<day>\d{2})/(?P<dock>[^/]+)/(?P<flight>[^/]+)(?:/|$)"
)
RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class SyncError(RuntimeError):
    """A source or delivery operation failed safely."""

    def __init__(self, message: str, *, authentication: bool = False):
        super().__init__(message)
        self.authentication = authentication


def utc_now() -> str:
    return dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sanitize_error(value: object) -> str:
    """Return bounded operator text with credential-shaped values removed."""
    text = str(value or "").replace("\x00", "")
    text = AWS_ACCESS_KEY.sub("[ACCESS_KEY_ID_REDACTED]", text)
    text = SECRET_VALUE.sub("[REDACTED]", text)
    return " ".join(text.split())[:1000]


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_fingerprint(entry: dict[str, Any]) -> str:
    stable = {
        "size": int(entry.get("Size") or 0),
        "modified": str(entry.get("ModTime") or ""),
        "hashes": entry.get("Hashes") if isinstance(entry.get("Hashes"), dict) else {},
        "metadata": entry.get("Metadata") if isinstance(entry.get("Metadata"), dict) else {},
        "tier": str(entry.get("Tier") or ""),
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_etag(entry: dict[str, Any]) -> str:
    for mapping_name in ("Metadata", "Hashes"):
        mapping = entry.get(mapping_name)
        if not isinstance(mapping, dict):
            continue
        lowered = {str(key).lower(): str(value) for key, value in mapping.items()}
        for key in ("etag", "e-tag", "md5"):
            if lowered.get(key):
                return lowered[key].strip('"')
    return ""


def source_metadata(entry: dict[str, Any]) -> dict[str, str]:
    value = entry.get("Metadata")
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def run_rclone_with_metadata_fallback(
    command: Sequence[str], *, runner: RunCommand
) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Run rclone, retrying without optional metadata on older installations."""
    completed = runner(
        list(command), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    detail = completed.stderr or completed.stdout or ""
    if (
        completed.returncode
        and "--metadata" in command
        and UNSUPPORTED_METADATA_FLAG.search(detail)
    ):
        compatible = [value for value in command if value != "--metadata"]
        return (
            runner(
                compatible,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ),
            False,
        )
    return completed, True


def safe_local_relative_path(key: str) -> tuple[Path, bool]:
    """Map a source key losslessly, quarantining unsafe filesystem names."""
    unsafe = (
        not key
        or key.startswith("/")
        or "\\" in key
        or any(ord(character) < 32 for character in key)
    )
    parts = key.split("/")
    unsafe = unsafe or any(part in {"", ".", ".."} for part in parts)
    if not unsafe:
        pure = PurePosixPath(key)
        return Path(*pure.parts), False
    key_hash = hashlib.sha256(key.encode("utf-8", errors="surrogatepass")).hexdigest()
    return Path("_unsafe_keys") / key_hash / "payload", True


def parse_flight(key: str) -> dict[str, str] | None:
    match = DATE_PATH.search(key)
    if not match:
        return None
    value = match.groupdict()
    value["date"] = f"{value['year']}-{value['month']}-{value['day']}"
    return value


def classify(key: str, config: dict[str, Any]) -> tuple[str, str]:
    flight = parse_flight(key)
    classification = config.get("classification")
    if not isinstance(classification, dict) or flight is None:
        return "unknown", "no reliable campaign mapping"
    dock_map = classification.get("dock_ids")
    flight_map = classification.get("flight_ids")
    dock_value = dock_map.get(flight["dock"]) if isinstance(dock_map, dict) else None
    flight_value = flight_map.get(flight["flight"]) if isinstance(flight_map, dict) else None
    if dock_value and flight_value and dock_value != flight_value:
        return "unknown", "dock and flight classifications conflict"
    value = flight_value or dock_value
    if value:
        return str(value), "configured dock/flight mapping"
    return "unknown", "no reliable campaign mapping"


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS object_versions (
            source_key TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            source_size INTEGER NOT NULL,
            source_modified TEXT NOT NULL,
            source_etag TEXT NOT NULL,
            source_hashes_json TEXT NOT NULL,
            local_relative_path TEXT NOT NULL,
            local_sha256 TEXT NOT NULL,
            campaign_classification TEXT NOT NULL,
            classification_reason TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            canonical INTEGER NOT NULL,
            archive_enqueued INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (source_key, source_fingerprint)
        );
        CREATE INDEX IF NOT EXISTS object_versions_source_key
            ON object_versions(source_key);
        CREATE INDEX IF NOT EXISTS object_versions_archive_pending
            ON object_versions(archive_enqueued);
        CREATE TABLE IF NOT EXISTS sync_runs (
            attempted_at TEXT PRIMARY KEY,
            finished_at TEXT NOT NULL,
            state TEXT NOT NULL,
            upstream_objects INTEGER NOT NULL,
            ingested_objects INTEGER NOT NULL,
            bytes_transferred INTEGER NOT NULL,
            failure_count INTEGER NOT NULL
        );
        """
    )
    return connection


def list_upstream(
    config: dict[str, Any],
    credential_path: Path,
    *,
    runner: RunCommand = subprocess.run,
) -> tuple[list[dict[str, Any]], bool]:
    remote = str(config["source_remote"])
    bucket = str(config["source_bucket"])
    command = [
        str(config.get("rclone_binary", "/usr/bin/rclone")),
        "lsjson",
        f"{remote}:{bucket}",
        f"--config={credential_path}",
        f"--s3-region={config['source_region']}",
        "--recursive",
        "--files-only",
        "--hash",
        "--metadata",
        "--fast-list",
    ]
    completed, metadata_supported = run_rclone_with_metadata_fallback(
        command, runner=runner
    )
    if completed.returncode:
        detail = sanitize_error(completed.stderr or completed.stdout or "upstream listing failed")
        raise SyncError(detail, authentication=bool(AUTH_FAILURE.search(detail)))
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SyncError(f"invalid rclone inventory JSON: {exc}") from exc
    if not isinstance(value, list):
        raise SyncError("rclone inventory was not a JSON list")
    entries = [item for item in value if isinstance(item, dict) and not item.get("IsDir")]
    for entry in entries:
        key = entry.get("Path")
        if not isinstance(key, str) or not key:
            raise SyncError("upstream inventory contained an object without a Path")
    return entries, metadata_supported


def download_object(
    config: dict[str, Any],
    credential_path: Path,
    key: str,
    destination: Path,
    *,
    runner: RunCommand = subprocess.run,
) -> None:
    remote = str(config["source_remote"])
    bucket = str(config["source_bucket"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(config.get("rclone_binary", "/usr/bin/rclone")),
        "copyto",
        f"{remote}:{bucket}/{key}",
        str(destination),
        f"--config={credential_path}",
        f"--s3-region={config['source_region']}",
        "--metadata",
        "--no-traverse",
        "--retries=4",
        "--low-level-retries=10",
        "--contimeout=30s",
        "--timeout=10m",
    ]
    completed, _metadata_supported = run_rclone_with_metadata_fallback(
        command, runner=runner
    )
    if completed.returncode:
        detail = sanitize_error(completed.stderr or completed.stdout or f"download failed for {key}")
        raise SyncError(detail, authentication=bool(AUTH_FAILURE.search(detail)))


def existing_version(
    connection: sqlite3.Connection, key: str, fingerprint: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM object_versions WHERE source_key = ? AND source_fingerprint = ?",
        (key, fingerprint),
    ).fetchone()


def versions_for_key(connection: sqlite3.Connection, key: str) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM object_versions WHERE source_key = ? ORDER BY ingested_at",
            (key,),
        )
    )


def record_version(
    connection: sqlite3.Connection,
    *,
    entry: dict[str, Any],
    fingerprint: str,
    local_relative_path: Path,
    local_sha256: str,
    classification: str,
    classification_reason: str,
    canonical: bool,
    timestamp: str,
) -> None:
    hashes = entry.get("Hashes") if isinstance(entry.get("Hashes"), dict) else {}
    connection.execute(
        """
        INSERT INTO object_versions (
            source_key, source_fingerprint, source_size, source_modified,
            source_etag, source_hashes_json, local_relative_path, local_sha256,
            campaign_classification, classification_reason, first_seen_at,
            last_seen_at, ingested_at, canonical, archive_enqueued
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(source_key, source_fingerprint) DO UPDATE SET
            last_seen_at = excluded.last_seen_at
        """,
        (
            str(entry["Path"]),
            fingerprint,
            int(entry.get("Size") or 0),
            str(entry.get("ModTime") or ""),
            source_etag(entry),
            json.dumps(hashes, sort_keys=True),
            local_relative_path.as_posix(),
            local_sha256,
            classification,
            classification_reason,
            timestamp,
            timestamp,
            timestamp,
            int(canonical),
        ),
    )
    connection.commit()


def ingest_entry(
    config: dict[str, Any],
    credential_path: Path,
    connection: sqlite3.Connection,
    entry: dict[str, Any],
    *,
    runner: RunCommand = subprocess.run,
    timestamp: str | None = None,
) -> dict[str, Any]:
    timestamp = timestamp or utc_now()
    raw_root = Path(config["raw_root"])
    key = str(entry["Path"])
    fingerprint = source_fingerprint(entry)
    found = existing_version(connection, key, fingerprint)
    if found is not None and (raw_root / found["local_relative_path"]).is_file():
        connection.execute(
            "UPDATE object_versions SET last_seen_at = ? WHERE source_key = ? AND source_fingerprint = ?",
            (timestamp, key, fingerprint),
        )
        connection.commit()
        return {
            "outcome": "unchanged",
            "source_key": key,
            "source_fingerprint": fingerprint,
            "local_relative_path": found["local_relative_path"],
            "bytes_transferred": 0,
            "campaign_classification": found["campaign_classification"],
        }

    relative, quarantined = safe_local_relative_path(key)
    existing_canonical = raw_root / relative
    prior = versions_for_key(connection, key)
    canonical = not prior and not existing_canonical.exists()
    if canonical:
        final = raw_root / relative
    else:
        key_hash = hashlib.sha256(key.encode("utf-8", errors="surrogatepass")).hexdigest()
        name = relative.name or "payload"
        final = raw_root / "_upstream_revisions" / key_hash / fingerprint / name

    final.parent.mkdir(parents=True, exist_ok=True)
    temporary = final.with_name(f".{final.name}.partial-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    try:
        download_object(config, credential_path, key, temporary, runner=runner)
        if not temporary.is_file():
            raise SyncError(f"download command did not create a file for {key}")
        expected_size = int(entry.get("Size") or 0)
        actual_size = temporary.stat().st_size
        if actual_size != expected_size:
            raise SyncError(
                f"size mismatch for {key}: expected {expected_size}, received {actual_size}"
            )
        local_sha256 = sha256_file(temporary)

        duplicate = next((row for row in prior if row["local_sha256"] == local_sha256), None)
        if duplicate is not None and (raw_root / duplicate["local_relative_path"]).is_file():
            temporary.unlink(missing_ok=True)
            local_relative_path = Path(duplicate["local_relative_path"])
            canonical = bool(duplicate["canonical"])
            outcome = "same_content_new_source_version"
        elif not prior and existing_canonical.is_file() and (
            existing_canonical.stat().st_size == actual_size
            and sha256_file(existing_canonical) == local_sha256
        ):
            temporary.unlink(missing_ok=True)
            local_relative_path = relative
            canonical = True
            outcome = "adopted_existing"
        else:
            temporary.replace(final)
            local_relative_path = final.relative_to(raw_root)
            outcome = "ingested" if canonical else "upstream_revision"

        campaign, reason = classify(key, config)
        if quarantined:
            reason = f"{reason}; unsafe source key quarantined"
        record_version(
            connection,
            entry=entry,
            fingerprint=fingerprint,
            local_relative_path=local_relative_path,
            local_sha256=local_sha256,
            classification=campaign,
            classification_reason=reason,
            canonical=canonical,
            timestamp=timestamp,
        )
        return {
            "record_type": "object",
            "outcome": outcome,
            "source_provider": "Menapia Ltd",
            "source_bucket": f"s3://{config['source_bucket']}/",
            "source_object_key": key,
            "source_fingerprint": fingerprint,
            "source_size": expected_size,
            "source_modified": str(entry.get("ModTime") or ""),
            "source_etag": source_etag(entry),
            "source_hashes": entry.get("Hashes") if isinstance(entry.get("Hashes"), dict) else {},
            "source_metadata": source_metadata(entry),
            "ingest_system": "AURORA Cloud",
            "ingest_timestamp": timestamp,
            "local_relative_path": local_relative_path.as_posix(),
            "local_sha256": local_sha256,
            "campaign_classification": campaign,
            "classification_reason": reason,
            "quarantined_source_key": quarantined,
            "bytes_transferred": actual_size,
        }
    finally:
        temporary.unlink(missing_ok=True)


def pending_archive_paths(connection: sqlite3.Connection, raw_root: Path) -> list[str]:
    rows = connection.execute(
        """
        SELECT DISTINCT local_relative_path
        FROM object_versions
        WHERE archive_enqueued = 0
        ORDER BY local_relative_path
        """
    )
    return [row["local_relative_path"] for row in rows if (raw_root / row["local_relative_path"]).is_file()]


def enqueue_archive(
    config: dict[str, Any],
    connection: sqlite3.Connection,
    *,
    runner: RunCommand = subprocess.run,
) -> int:
    raw_root = Path(config["raw_root"])
    paths = pending_archive_paths(connection, raw_root)
    if not paths:
        return 0
    command_path = Path(config.get("archive_dispatch_command", "/usr/local/bin/aurora-archive-dispatch"))
    if not command_path.exists() and bool(config.get("archive_dispatch_required", True)):
        raise SyncError(f"archive dispatcher is missing: {command_path}")
    if not command_path.exists():
        return 0
    state_root = Path(config["state_database"]).parent
    state_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=state_root, prefix="archive-paths-", delete=False) as handle:
        queue_path = Path(handle.name)
        for path in paths:
            handle.write(path.encode("utf-8") + b"\0")
    try:
        command = [
            str(command_path),
            "enqueue",
            "--job",
            "raw",
            "--base",
            str(raw_root),
            "--files-from",
            str(queue_path),
            "--null",
        ]
        completed = runner(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if completed.returncode:
            raise SyncError(sanitize_error(completed.stderr or completed.stdout or "archive enqueue failed"))
        connection.executemany(
            "UPDATE object_versions SET archive_enqueued = 1 WHERE local_relative_path = ?",
            [(path,) for path in paths],
        )
        connection.commit()
        return len(paths)
    finally:
        queue_path.unlink(missing_ok=True)


def write_manifest(root: Path, attempted_at: str, records: Iterable[dict[str, Any]], summary: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    stamp = attempted_at.replace(":", "").replace("-", "")
    destination = root / f"{stamp}.jsonl"
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"record_type": "run", **summary}, sort_keys=True) + "\n")
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    temporary.replace(destination)
    return destination


def credential_status(expires_on: str, now: dt.datetime | None = None) -> dict[str, Any]:
    today = (now or dt.datetime.now(UTC)).date()
    try:
        expiry = dt.date.fromisoformat(expires_on)
    except ValueError:
        return {"expires_on": expires_on, "days_remaining": None, "level": "unknown"}
    days = (expiry - today).days
    level = "red" if days < 0 or days <= 7 else "amber" if days <= 30 else "green"
    return {"expires_on": expires_on, "days_remaining": days, "level": level}


def latest_flight(entries: Sequence[dict[str, Any]]) -> dict[str, str] | None:
    flights = [value for entry in entries if (value := parse_flight(str(entry["Path"]))) is not None]
    if not flights:
        return None
    return max(flights, key=lambda value: (value["date"], value["flight"]))


def run_sync(
    config: dict[str, Any],
    credential_path: Path,
    *,
    runner: RunCommand = subprocess.run,
    inventory_only: bool = False,
    max_objects: int | None = None,
    include_keys: set[str] | None = None,
) -> int:
    attempted_at = utc_now()
    raw_root = Path(config["raw_root"])
    state_path = Path(config["state_database"])
    status_path = Path(config["status_path"])
    manifest_root = Path(config["manifest_root"])
    lock_path = Path(config["lock_path"])
    previous = load_json(status_path)
    credential = credential_status(str(config["credential_expires_on"]))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        status: dict[str, Any] = {
            "schema_version": 1,
            "source_id": "menapia",
            "source_description": "Menapia autonomous drone flight data",
            "source_bucket": f"s3://{config['source_bucket']}/",
            "source_region": str(config["source_region"]),
            "attempted_at": attempted_at,
            "last_success_at": previous.get("last_success_at"),
            "state": "running",
            "credential": credential,
            "upstream_objects_examined": 0,
            "upstream_bytes_examined": 0,
            "candidate_objects": 0,
            "pending_objects": 0,
            "new_objects_ingested": 0,
            "upstream_revisions_ingested": 0,
            "unchanged_objects": 0,
            "unclassified_objects": 0,
            "bytes_transferred": 0,
            "archive_paths_enqueued": 0,
            "authentication_failure": False,
            "failure_count": 0,
            "failures": [],
            "latest_source_flight": None,
            "flight_path_objects": 0,
            "non_flight_path_objects": 0,
            "source_path_samples": [],
            "source_path_warnings": [],
            "source_metadata_listing": None,
        }
        atomic_json(status_path, status)
        records: list[dict[str, Any]] = []
        connection = connect_database(state_path)
        try:
            try:
                entries, metadata_supported = list_upstream(
                    config, credential_path, runner=runner
                )
            except SyncError as exc:
                status["state"] = "failed"
                status["authentication_failure"] = exc.authentication
                status["failure_count"] = 1
                status["failures"] = [sanitize_error(exc)]
                status["finished_at"] = utc_now()
                atomic_json(status_path, status)
                write_manifest(manifest_root, attempted_at, records, status)
                return 1

            status["source_metadata_listing"] = (
                "full" if metadata_supported else "basic_compatibility"
            )
            if not metadata_supported:
                status["source_path_warnings"].append(
                    "Installed rclone does not support optional S3 metadata listing; "
                    "provenance retains object size, modification time, and available hashes"
                )

            if include_keys:
                entries = [entry for entry in entries if str(entry["Path"]) in include_keys]
            entries.sort(key=lambda entry: (str(entry.get("ModTime") or ""), str(entry["Path"])), reverse=True)
            status["upstream_objects_examined"] = len(entries)
            status["upstream_bytes_examined"] = sum(
                int(entry.get("Size") or 0) for entry in entries
            )
            status["latest_source_flight"] = latest_flight(entries)
            status["source_path_samples"] = [
                str(entry["Path"]) for entry in entries[:20]
            ]
            status["flight_path_objects"] = sum(
                parse_flight(str(entry["Path"])) is not None for entry in entries
            )
            status["non_flight_path_objects"] = (
                len(entries) - status["flight_path_objects"]
            )
            if status["non_flight_path_objects"]:
                status["source_path_warnings"].append(
                    f"{status['non_flight_path_objects']} object(s) do not match the expected "
                    "drone-uploads/YYYY/MM/DD/<dock-id>/<flight-id>/ hierarchy; "
                    "they remain preserved and unclassified"
                )

            candidates: list[dict[str, Any]] = []
            for entry in entries:
                key = str(entry["Path"])
                fingerprint = source_fingerprint(entry)
                row = existing_version(connection, key, fingerprint)
                if row is not None and (raw_root / row["local_relative_path"]).is_file():
                    status["unchanged_objects"] += 1
                    connection.execute(
                        "UPDATE object_versions SET last_seen_at = ? WHERE source_key = ? AND source_fingerprint = ?",
                        (attempted_at, key, fingerprint),
                    )
                else:
                    candidates.append(entry)
            connection.commit()
            status["candidate_objects"] = len(candidates)

            limit = int(config.get("max_objects_per_run", 500) if max_objects is None else max_objects)
            selected = candidates if limit <= 0 else candidates[:limit]
            status["pending_objects"] = max(len(candidates) - len(selected), 0)

            if not inventory_only:
                for entry in selected:
                    try:
                        record = ingest_entry(
                            config,
                            credential_path,
                            connection,
                            entry,
                            runner=runner,
                            timestamp=attempted_at,
                        )
                    except SyncError as exc:
                        status["failure_count"] += 1
                        status["authentication_failure"] = bool(
                            status["authentication_failure"] or exc.authentication
                        )
                        status["failures"].append(
                            sanitize_error(f"{entry['Path']}: {exc}")
                        )
                        continue
                    records.append(record)
                    if record["outcome"] == "unchanged":
                        status["unchanged_objects"] += 1
                        continue
                    status["bytes_transferred"] += int(record["bytes_transferred"])
                    if record["campaign_classification"] == "unknown":
                        status["unclassified_objects"] += 1
                    if record["outcome"] == "upstream_revision":
                        status["upstream_revisions_ingested"] += 1
                    else:
                        status["new_objects_ingested"] += 1

                try:
                    status["archive_paths_enqueued"] = enqueue_archive(
                        config, connection, runner=runner
                    )
                except SyncError as exc:
                    status["failure_count"] += 1
                    status["failures"].append(sanitize_error(exc))

            status["unclassified_objects"] = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT source_key)
                    FROM object_versions
                    WHERE campaign_classification = 'unknown'
                    """
                ).fetchone()[0]
            )
            status["tracked_object_versions"] = int(
                connection.execute("SELECT COUNT(*) FROM object_versions").fetchone()[0]
            )

            if inventory_only:
                status["state"] = "inventory_only"
            elif status["failure_count"]:
                status["state"] = "partial_failure" if records else "failed"
            elif status["pending_objects"]:
                status["state"] = "success_with_backlog"
                status["last_success_at"] = attempted_at
            else:
                status["state"] = "success"
                status["last_success_at"] = attempted_at
            status["finished_at"] = utc_now()
            connection.execute(
                """
                INSERT INTO sync_runs (
                    attempted_at, finished_at, state, upstream_objects,
                    ingested_objects, bytes_transferred, failure_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempted_at,
                    status["finished_at"],
                    status["state"],
                    status["upstream_objects_examined"],
                    status["new_objects_ingested"] + status["upstream_revisions_ingested"],
                    status["bytes_transferred"],
                    status["failure_count"],
                ),
            )
            connection.commit()
            write_manifest(manifest_root, attempted_at, records, status)
            atomic_json(status_path, status)
            return 1 if status["failure_count"] else 0
        finally:
            connection.close()


def resolve_credential_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    credential_directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if credential_directory:
        return Path(credential_directory) / "menapia-rclone.conf"
    raise SyncError("Menapia credential was not supplied through systemd LoadCredential")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--credential", help="Test/manual credential path; systemd uses LoadCredential")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--max-objects", type=int)
    parser.add_argument("--include-key", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_json(args.config)
    if not config:
        print(f"Invalid or missing configuration: {args.config}", file=sys.stderr)
        return 2
    try:
        credential_path = resolve_credential_path(args.credential)
    except SyncError as exc:
        print(sanitize_error(exc), file=sys.stderr)
        return 2
    if not credential_path.is_file():
        print(f"Menapia credential file is missing: {credential_path}", file=sys.stderr)
        return 2
    return run_sync(
        config,
        credential_path,
        inventory_only=args.inventory_only,
        max_objects=args.max_objects,
        include_keys=set(args.include_key) or None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
