"""Bounded, resumable S3 listings for one immutable verification epoch.

Only a validated ListObjectsV2 page and its cursor are committed together. The
database is private recovery state, never canonical archive evidence. No object
writes or SDK-managed network retries occur in this module.
"""

from __future__ import annotations

import configparser
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import tempfile
from urllib.parse import urlsplit


COMMON_EXCLUDES = [
    "**/.git/**", "**/.venv/**", "**/__pycache__/**", "**/.cache/**",
    "**/*.lock", "**/*.partial", "**/*.part", "**/*.tmp", "**/*-wal",
    "**/*-shm", "**/logs/**", "**/*backup*.zarr/**", "**/*schema-backup*.zarr/**",
]


class InventoryError(RuntimeError):
    """Sanitized failure suitable for the coordinator's persisted status."""

    def __init__(self, message: str, error_class: str = "transient", *,
                 restart_epoch: bool = False):
        super().__init__(message)
        self.error_class = error_class
        self.restart_epoch = restart_epoch


def recovery_root(config: dict) -> Path:
    return Path(config["recovery_root"] if config.get("recovery_root") else
                Path(config["manifest_root"]) / "recovery")


def check_resources(config: dict, extra_bytes: int = 0) -> dict:
    """Include all epochs, frozen snapshots, queue DBs and journals in quota."""
    root = recovery_root(config)
    used = 0
    if root.exists():
        for directory, _, names in os.walk(root, followlinks=False):
            for name in names:
                try:
                    used += (Path(directory) / name).lstat().st_size
                except FileNotFoundError:
                    continue
    available_root = root
    while not available_root.exists():
        available_root = available_root.parent
    free = shutil.disk_usage(available_root).free
    cap = int(config.get("recovery_cache_max_bytes", 10 * 1024**3))
    reserve = int(config.get("recovery_free_reserve_bytes", 50 * 1024**3))
    if used + extra_bytes > cap:
        raise InventoryError("Recovery storage quota exhausted", "resource")
    if free - extra_bytes < reserve:
        raise InventoryError("Recovery filesystem free-space reserve reached", "resource")
    return {"used_bytes": used, "max_bytes": cap, "free_bytes": free,
            "reserve_bytes": reserve}


@contextmanager
def resource_lock(config: dict, extra_bytes: int = 0):
    """Serialize checkpoint growth checks across both worker processes."""
    root = recovery_root(config)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / "storage.lock"
    if not lock_path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".storage-lock-", dir=root)
        try:
            os.fchmod(fd, 0o660)
            if os.geteuid() == 0:
                owner = root.stat()
                os.fchown(fd, owner.st_uid, owner.st_gid)
            try:
                os.link(temporary, lock_path)
            except FileExistsError:
                pass
        finally:
            os.close(fd)
            os.unlink(temporary)
    elif os.geteuid() == 0:
        owner = root.stat()
        os.chown(lock_path, owner.st_uid, owner.st_gid)
        lock_path.chmod(0o660)
    with lock_path.open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        check_resources(config, extra_bytes)
        yield


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _excluded(path: str, patterns: list[str]) -> bool:
    value = path.lstrip("/")
    for pattern in patterns:
        normalized = pattern.lstrip("/")
        variants = {normalized, normalized.replace("**/", "*/")}
        if normalized.startswith("**/"):
            variants.add(normalized[3:])
        if any(fnmatch.fnmatch(value, variant) for variant in variants):
            return True
    return False


def _client(config: dict):
    """Use only the nominated rclone remote, never an AWS credential chain."""
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    credential = (Path(credential_dir) / "s3-rclone-config" if credential_dir else
                  Path(config.get("rclone_config", "/etc/aurora-object-store/rclone.conf")))
    try:
        if credential.stat().st_mode & 0o077:
            raise InventoryError("S3 credential permissions must be owner-only", "config")
        parser = configparser.ConfigParser(interpolation=None)
        with credential.open() as stream:
            parser.read_file(stream)
        remote = parser[str(config["remote"])]
        access = remote.get("access_key_id", "").strip()
        secret = remote.get("secret_access_key", "").strip()
        endpoint = remote.get("endpoint", "").strip()
        if endpoint and "://" not in endpoint:
            endpoint = "https://" + endpoint
        endpoint_parts = urlsplit(endpoint)
        if (remote.get("type", "").strip().lower() != "s3" or
                remote.getboolean("env_auth", fallback=False) or not access or not secret or
                endpoint_parts.scheme != "https" or not endpoint_parts.hostname or
                endpoint_parts.username or endpoint_parts.password or endpoint_parts.query or
                endpoint_parts.fragment):
            raise InventoryError("Explicit S3 credentials and an HTTPS endpoint are required", "config")
        region = config.get("s3_region") or remote.get("region") or "us-east-1"
        style = config.get("s3_addressing_style", "path")
        if style not in {"path", "virtual"}:
            raise InventoryError("Unsupported S3 addressing style", "config")
        import boto3
        from botocore.config import Config
        return boto3.client(
            "s3", endpoint_url=endpoint, region_name=region,
            aws_access_key_id=access, aws_secret_access_key=secret,
            aws_session_token=remote.get("session_token") or None,
            config=Config(
                connect_timeout=int(config.get("recovery_s3_connect_timeout_seconds", 30)),
                read_timeout=int(config.get("recovery_s3_read_timeout_seconds", 120)),
                retries={"total_max_attempts": 1, "mode": "standard"},
                max_pool_connections=2, signature_version="s3v4",
                s3={"addressing_style": style},
            ),
        )
    except InventoryError:
        raise
    except (OSError, KeyError, ValueError, configparser.Error):
        raise InventoryError("Cannot load the restricted S3 configuration", "config") from None
    except ImportError:
        raise InventoryError("Pinned S3 reader runtime is unavailable", "config") from None


def _request_error(exc: Exception, *, has_cursor: bool) -> tuple[InventoryError, bool]:
    # Do not persist SDK messages: they may contain request URLs or credentials.
    response = getattr(exc, "response", {})
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    code = str(error.get("Code", ""))
    metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
    status = metadata.get("HTTPStatusCode")
    if has_cursor and code in {"InvalidToken", "InvalidContinuationToken", "InvalidArgument"}:
        return InventoryError("S3 continuation token expired"), True
    if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken",
                "TokenRefreshRequired", "InvalidToken"} or status in {401, 403}:
        return InventoryError("S3 authentication or authorization failed", "auth"), False
    if code in {"NoSuchBucket", "InvalidBucketName", "InvalidRequest", "AuthorizationHeaderMalformed"}:
        return InventoryError("S3 bucket or request configuration is invalid", "config"), False
    if exc.__class__.__name__ in {"NoCredentialsError", "PartialCredentialsError", "ParamValidationError"}:
        return InventoryError("S3 reader configuration is incomplete", "config"), False
    status_text = f" (HTTP {status})" if type(status) is int and 100 <= status < 600 else ""
    return InventoryError(f"S3 listing unavailable{status_text}; checkpoint retained"), False


class PagedS3Lister:
    def __init__(self, config: dict, epoch_dir: Path | str, verification_id: str, client=None):
        self.config = config
        self.epoch_dir = Path(epoch_dir)
        self.verification_id = verification_id
        self.client = client
        self.db_path = self.epoch_dir / "s3.sqlite"
        self._db_lock = threading.RLock()
        self._stop = threading.Event()
        self.page_size = min(1000, max(1, int(config.get("recovery_s3_page_size", 1000))))
        if not verification_id or self.epoch_dir.is_symlink():
            raise InventoryError("Invalid verification epoch", "config")
        if not self.epoch_dir.resolve().is_relative_to(recovery_root(config).resolve()):
            raise InventoryError("Epoch must be inside the recovery root", "config")
        self.db = None
        with resource_lock(config, 256 * 1024):
            self.epoch_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                self.db = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
                os.chmod(self.db_path, 0o600)
                self.db.row_factory = sqlite3.Row
                self.db.execute("PRAGMA journal_mode=DELETE")
                self.db.execute("PRAGMA synchronous=FULL")
                if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("integrity")
                self.db.executescript("""
                    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS shards (
                        prefix TEXT PRIMARY KEY, mode TEXT NOT NULL, cursor TEXT,
                        done INTEGER NOT NULL DEFAULT 0, expanded INTEGER NOT NULL DEFAULT 0,
                        pages INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS entries (
                        shard TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,
                        size INTEGER, mtime TEXT, PRIMARY KEY(shard, key, kind));
                    CREATE TABLE IF NOT EXISTS pages (
                        shard TEXT NOT NULL, token TEXT NOT NULL, next_token TEXT,
                        digest TEXT NOT NULL, PRIMARY KEY(shard, token));
                """)
            except sqlite3.DatabaseError:
                if self.db is not None:
                    self.db.close()
                raise InventoryError("Corrupt S3 checkpoint; fresh epoch required", restart_epoch=True) from None

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _tree_excluded(self, relative: str) -> bool:
        return _excluded(relative, self.patterns) or _excluded(
            f"{relative.rstrip('/')}/__inventory_probe__", self.patterns)

    def _mode(self, relative: str) -> str:
        prefix = relative.rstrip("/")
        if prefix not in self.job.get("sharded_prefixes", []) and not self.job.get("shard_all_prefixes", False):
            return "leaf"
        paths = [key[len(prefix) + 1:] for key in self.local if key.startswith(prefix + "/")]
        return "leaf" if paths and all("/" not in path for path in paths) else "level1"

    def _initialize(self, job: dict, local: dict):
        self.job, self.local = job, local
        self.patterns = COMMON_EXCLUDES + job.get("exclude", [])
        self.destination = str(job["destination"]).strip("/")
        self.base = self.destination + "/" if self.destination else ""
        if any(part in {".", ".."} for part in self.destination.split("/")):
            raise InventoryError("Unsafe S3 destination", "config")
        identity = _digest({"schema": 1, "verification_id": self.verification_id,
                            "config": {key: value for key, value in self.config.items() if not key.startswith("_")},
                            "job": job, "local": local})
        with self._db_lock, resource_lock(self.config, 256 * 1024), self.db:
            previous = self.db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
            if previous and previous[0] != identity:
                raise InventoryError("Checkpoint epoch or frozen source changed", restart_epoch=True)
            if previous:
                return
            self.db.execute("INSERT INTO metadata VALUES ('identity', ?)", (identity,))
            self.db.execute("INSERT INTO metadata VALUES ('verification_id', ?)", (self.verification_id,))
            local_prefixes = {key.split("/", 1)[0] for key in local if "/" in key}
            root_files = {key for key in local if "/" not in key}
            sharded = set(job.get("sharded_prefixes", []))
            source_covers_root = bool(local_prefixes) and not root_files and (
                (bool(sharded) and local_prefixes <= sharded) or job.get("shard_all_prefixes", False))
            if not source_covers_root:
                self.db.execute("INSERT INTO shards(prefix, mode) VALUES (?, 'root')", (self.base,))
            for prefix in sorted(local_prefixes):
                if not self._tree_excluded(prefix):
                    self.db.execute("INSERT OR IGNORE INTO shards(prefix, mode) VALUES (?, ?)",
                                    (self.base + prefix + "/", self._mode(prefix)))

    def _validate_page(self, response: dict, prefix: str, mode: str, cursor: str | None):
        malformed = InventoryError("Malformed S3 listing page; checkpoint retained")
        if not isinstance(response, dict) or type(response.get("IsTruncated")) is not bool:
            raise malformed
        metadata = response.get("ResponseMetadata", {})
        if not isinstance(metadata, dict) or metadata.get("HTTPStatusCode", 200) != 200:
            raise malformed
        contents = response.get("Contents", [])
        prefixes = response.get("CommonPrefixes", [])
        if not isinstance(contents, list) or not isinstance(prefixes, list):
            raise malformed
        if len(contents) + len(prefixes) > self.page_size:
            raise malformed
        if "KeyCount" in response and (type(response["KeyCount"]) is not int or
                response["KeyCount"] != len(contents) + len(prefixes)):
            raise malformed
        for field, expected in (("Name", self.config["bucket"]), ("Prefix", prefix)):
            if field in response and response[field] != expected:
                raise malformed
        next_token = response.get("NextContinuationToken")
        if response["IsTruncated"]:
            if not isinstance(next_token, str) or not next_token or next_token == cursor:
                raise malformed
        elif next_token is not None:
            raise malformed
        if mode == "leaf" and prefixes:
            raise malformed
        rows = []
        seen = set()
        for item in contents:
            if not isinstance(item, dict):
                raise malformed
            key, size, modified = item.get("Key"), item.get("Size"), item.get("LastModified")
            if (not isinstance(key, str) or not key.startswith(prefix) or type(size) is not int or size < 0 or
                    not isinstance(modified, dt.datetime) or modified.tzinfo is None):
                raise malformed
            relative = key[len(prefix):]
            if mode != "leaf" and "/" in relative.rstrip("/"):
                raise malformed
            if key in seen:
                raise malformed
            seen.add(key)
            # rclone files-only omits zero-byte directory markers.
            if key.endswith("/") and size == 0:
                continue
            rows.append((key, "object", size, modified.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")))
        for item in prefixes:
            if not isinstance(item, dict):
                raise malformed
            key = item.get("Prefix")
            if (not isinstance(key, str) or not key.startswith(prefix) or key == prefix or
                    not key.endswith("/") or "/" in key[len(prefix):-1] or key in seen):
                raise malformed
            seen.add(key)
            rows.append((key, "prefix", None, None))
        return rows, next_token, not response["IsTruncated"]

    def _commit_page(self, prefix, cursor, rows, next_token, done):
        digest = _digest([rows, next_token, done])
        # Reserve space for btree growth and the SQLite rollback journal before
        # writes, under the cross-worker quota lock. Count journal files too.
        growth = max(256 * 1024, len(rows) * 24 * 1024 +
                     len(json.dumps(rows).encode()) * 4 + 256 * 1024)
        with self._db_lock, resource_lock(self.config, growth), self.db:
            state = self.db.execute("SELECT cursor, done FROM shards WHERE prefix=?", (prefix,)).fetchone()
            prior = self.db.execute("SELECT digest FROM pages WHERE shard=? AND token=?", (prefix, cursor or "")).fetchone()
            if prior:
                if prior[0] != digest:
                    raise InventoryError("Conflicting S3 page replay; fresh epoch required", restart_epoch=True)
                return
            if not state or state["cursor"] != cursor or state["done"]:
                raise InventoryError("S3 checkpoint cursor conflict; fresh epoch required", restart_epoch=True)
            if next_token and self.db.execute("SELECT 1 FROM pages WHERE shard=? AND token=?", (prefix, next_token)).fetchone():
                raise InventoryError("S3 continuation token cycle; checkpoint retained")
            for key, kind, size, mtime in rows:
                prior_row = self.db.execute("SELECT size, mtime FROM entries WHERE shard=? AND key=? AND kind=?",
                                            (prefix, key, kind)).fetchone()
                if prior_row and tuple(prior_row) != (size, mtime):
                    raise InventoryError("S3 object changed across listing pages; fresh epoch required", restart_epoch=True)
                self.db.execute("INSERT OR IGNORE INTO entries VALUES (?, ?, ?, ?, ?)",
                                (prefix, key, kind, size, mtime))
            self.db.execute("INSERT INTO pages VALUES (?, ?, ?, ?)", (prefix, cursor or "", next_token, digest))
            self.db.execute("UPDATE shards SET cursor=?, done=?, pages=pages+1 WHERE prefix=?", (next_token, int(done), prefix))

    def _reset_shard(self, prefix: str):
        with self._db_lock, resource_lock(self.config, self.db_path.stat().st_size + 256 * 1024), self.db:
            # Descendants are enqueued only after a parent finishes. A failed
            # continuation can therefore invalidate exactly this shard.
            self.db.execute("DELETE FROM entries WHERE shard=?", (prefix,))
            self.db.execute("DELETE FROM pages WHERE shard=?", (prefix,))
            self.db.execute("UPDATE shards SET cursor=NULL, done=0, expanded=0, pages=0 WHERE prefix=?", (prefix,))

    def _validate_epoch(self):
        deadline = self.config.get("_recovery_deadline")
        if deadline is not None and time.time() >= float(deadline):
            raise InventoryError("S3 observation window expired; fresh epoch required", restart_epoch=True)
        callback = self.config.get("_validate_epoch")
        if callback is not None and callback() is False:
            raise InventoryError("S3 observation invalidated; fresh epoch required", restart_epoch=True)

    def _list_shard(self, prefix: str, mode: str):
        reset_used = False
        while not self._stop.is_set():
            self._validate_epoch()
            with self._db_lock:
                state = self.db.execute("SELECT cursor, done FROM shards WHERE prefix=?", (prefix,)).fetchone()
            if state["done"]:
                return
            cursor = state["cursor"]
            check_resources(self.config)
            request = {"Bucket": self.config["bucket"], "Prefix": prefix, "MaxKeys": self.page_size}
            if mode != "leaf":
                request["Delimiter"] = "/"
            if cursor:
                request["ContinuationToken"] = cursor
            try:
                response = self.client.list_objects_v2(**request)
            except Exception as exc:
                error, invalid_token = _request_error(exc, has_cursor=bool(cursor))
                if invalid_token and not reset_used:
                    self._reset_shard(prefix)
                    reset_used = True
                    continue
                raise error from None
            self._validate_epoch()
            rows, next_token, done = self._validate_page(response, prefix, mode, cursor)
            self._commit_page(prefix, cursor, rows, next_token, done)
            callback = self.config.get("_progress_callback")
            if callback is not None:
                callback(self.progress())
            if done:
                return

    def _expand_completed(self):
        with self._db_lock:
            completed = self.db.execute("SELECT prefix, mode FROM shards WHERE done=1 AND expanded=0").fetchall()
            if not completed:
                return
            descendants = []
            for shard in completed:
                if shard["mode"] != "leaf":
                    for row in self.db.execute("SELECT key FROM entries WHERE shard=? AND kind='prefix'", (shard["prefix"],)).fetchall():
                        key = row["key"]
                        relative = key[len(self.base):].rstrip("/")
                        if self._tree_excluded(relative):
                            continue
                        mode = self._mode(relative) if shard["mode"] == "root" else (
                            "level2" if shard["mode"] == "level1" else "leaf")
                        descendants.append((key, mode))
            growth = (len(descendants) + len(completed)) * 24 * 1024 + 256 * 1024
            with resource_lock(self.config, growth), self.db:
                self.db.executemany("INSERT OR IGNORE INTO shards(prefix, mode) VALUES (?, ?)", descendants)
                self.db.executemany("UPDATE shards SET expanded=1 WHERE prefix=?", [(shard["prefix"],) for shard in completed])

    def inventory(self, job: dict, local: dict[str, dict]) -> dict[str, dict]:
        try:
            self._validate_epoch()
            self._initialize(job, local)
            if self.client is None:
                self.client = _client(self.config)
            self._stop.clear()
            failure = None
            with ThreadPoolExecutor(max_workers=2) as pool:
                active = {}
                while True:
                    self._expand_completed()
                    with self._db_lock:
                        pending = self.db.execute("SELECT prefix, mode FROM shards WHERE done=0 ORDER BY prefix").fetchall()
                    active_prefixes = set(active.values())
                    for shard in pending:
                        if len(active) == 2 or failure:
                            break
                        if shard["prefix"] not in active_prefixes:
                            active[pool.submit(self._list_shard, shard["prefix"], shard["mode"])] = shard["prefix"]
                    if not active:
                        break
                    finished, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in finished:
                        active.pop(future)
                        try:
                            future.result()
                        except Exception as exc:
                            failure = failure or exc
                            self._stop.set()
                if failure:
                    raise failure
            self._validate_epoch()
            result = {}
            for item in self.db.execute("SELECT key, size, mtime FROM entries WHERE kind='object'"):
                relative = item["key"][len(self.base):]
                if not relative or _excluded(relative, self.patterns):
                    continue
                row = {"relative_path": relative, "size": item["size"], "mtime": item["mtime"], "checksum": ""}
                if relative in result and result[relative] != row:
                    raise InventoryError("S3 overlapping shards disagree; fresh epoch required", restart_epoch=True)
                result[relative] = row
            return result
        except sqlite3.DatabaseError as exc:
            if "full" in str(exc).lower():
                raise InventoryError("Recovery checkpoint storage exhausted", "resource") from None
            raise InventoryError("Corrupt S3 checkpoint; fresh epoch required", restart_epoch=True) from None

    def progress(self) -> dict:
        with self._db_lock:
            total, completed, pages = self.db.execute("SELECT COUNT(*), COALESCE(SUM(done),0), COALESCE(SUM(pages),0) FROM shards").fetchone()
            objects = self.db.execute("SELECT COUNT(*) FROM entries WHERE kind='object'").fetchone()[0]
        return {"verification_id": self.verification_id, "shards_total": total,
                "shards_completed": completed, "pages_completed": pages,
                "objects_observed": objects}
