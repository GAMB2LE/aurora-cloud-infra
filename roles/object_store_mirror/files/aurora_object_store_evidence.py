"""Atomic archive evidence generations, reader pins, and family publication.

Remote collection never holds the commit lock. Completed family artifacts are
copied into staging before taking it; unchanged immutable artifacts are linked
while merging the latest generation. A reader pins one generation for its full
use, including retention candidate preparation.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import datetime as dt
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from types import SimpleNamespace
import uuid

GATE_STATE = Path("/var/lib/aurora-cloud/object-store-verification-gate/state.json")
GATE_NAME = "verification-gate.json"
ARTIFACT_SUFFIXES = ("local.tsv", "s3.tsv", "gws.tsv", "gws-source.tsv")


def _artifact_names(name):
    return {f"{name}-{suffix}" for suffix in ARTIFACT_SUFFIXES}


def _gate_module():
    path = Path(__file__).with_name("aurora-object-store-verification-gate.py")
    spec = importlib.util.spec_from_file_location("aurora_gate_evaluator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _fsync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: Path, data: bytes):
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _state_path(config):
    return Path(config.get("gate_state_path", GATE_STATE))


def _compatibility_gate(config, state):
    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".gate-", dir=path.parent)
    try:
        # Compatibility readers remain unprivileged. This is the same
        # non-secret evidence already published in readable generations.
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_json_bytes(state))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _commit_lock(root: Path, *, shared=False, wait=False):
    if not shared:
        root.mkdir(parents=True, exist_ok=True)
    lock = root / ".inventory.lock"
    if not shared and not lock.exists():
        fd, temporary = tempfile.mkstemp(prefix=".inventory-lock-", dir=root)
        try:
            os.fchmod(fd, 0o664)
            if os.geteuid() == 0:
                owner = root.stat()
                os.fchown(fd, owner.st_uid, owner.st_gid)
            # Only expose the inode after its access permissions and owner
            # are ready. Concurrent initializers link the same canonical
            # name; the loser discards its private candidate.
            try:
                os.link(temporary, lock)
            except FileExistsError:
                pass
        finally:
            os.close(fd)
            os.unlink(temporary)
    # flock needs no writes to the inode. A privileged initializer must not
    # make later unprivileged lock acquisition require write permission.
    with lock.open("rb") as handle:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(handle, mode | (0 if wait else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read(path: Path, config):
    raw = (path / "comparison.json").read_bytes()
    report = json.loads(raw)
    gate_path = path / GATE_NAME
    state = json.loads((gate_path if gate_path.exists() else _state_path(config)).read_bytes())
    digest = hashlib.sha256(raw).hexdigest()
    if state.get("report_sha256") != digest or state.get("last_generated_at") != report.get("generated_at"):
        raise ValueError("object-store gate does not describe the pinned inventory")
    return SimpleNamespace(path=path, report=report, gate=state, report_sha256=digest)


@contextmanager
def read_snapshot(config):
    """Yield .path/.report/.gate/.report_sha256; hold pin through artifact use.

    The short shared commit lock prevents cleanup between pointer resolution
    and acquiring the generation pin. Legacy mutable trees keep the commit
    lock until the context exits and remain fail-closed on a gate mismatch.
    """
    root = Path(config["manifest_root"])
    pin = None
    with _commit_lock(root, shared=True):
        latest = root / "latest"
        path = latest.resolve(strict=True)
        if latest.is_symlink():
            path.relative_to((root / "generations").resolve())
            pin = (path / ".pin.lock").open("rb")
            fcntl.flock(pin, fcntl.LOCK_SH)
        else:
            yield _read(path, config)
            return
    try:
        yield _read(path, config)
    finally:
        if pin is not None:
            fcntl.flock(pin, fcntl.LOCK_UN)
            pin.close()


def _exchange(first: Path, second: Path):
    """Atomically replace a legacy directory with a pointer, retaining both.

    Linux production uses renameat2(RENAME_EXCHANGE); macOS tests use
    renamex_np(RENAME_SWAP). Unsupported filesystems abort without renaming.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        call = libc.renamex_np
        call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = call(os.fsencode(first), os.fsencode(second), 2)
    elif hasattr(libc, "renameat2"):
        call = libc.renameat2
        call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = call(-100, os.fsencode(first), -100, os.fsencode(second), 2)
    else:
        raise OSError("atomic directory-to-pointer exchange is unsupported")
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _switch(root, generation):
    pointer = root / f".latest-pointer-{uuid.uuid4().hex}"
    pointer.symlink_to(generation.relative_to(root), target_is_directory=True)
    latest = root / "latest"
    try:
        if latest.exists() and not latest.is_symlink():
            _exchange(pointer, latest)
            # This is the unmodified legacy tree, retained for rollback. A
            # crash before rename leaves it at .latest-pointer-* for recovery.
            pointer.rename(root / f"legacy-latest-{uuid.uuid4().hex}")
        else:
            pointer.replace(latest)
        _fsync_dir(root)
    finally:
        if pointer.is_symlink():
            pointer.unlink()


def _load_previous(root, config):
    latest = root / "latest"
    if not (latest / "comparison.json").is_file():
        return None, {}, {}, None
    path = latest.resolve()
    raw = (path / "comparison.json").read_bytes()
    report = json.loads(raw)
    gate_path = path / GATE_NAME if (path / GATE_NAME).exists() else _state_path(config)
    previous = json.loads(gate_path.read_bytes()) if gate_path.is_file() else {}
    if previous.get("report_sha256") != hashlib.sha256(raw).hexdigest():
        # Legacy publication and its gate can lag. Never carry unbound streaks.
        previous = {}
    return path, report, previous, hashlib.sha256(raw).hexdigest()


def _merge_artifacts(path, stage, replaced):
    if path is None:
        return
    for source in path.iterdir():
        if not source.is_file() or source.name.startswith("."):
            continue
        if source.name in {"comparison.json", "comparison.md", "catalog.json", GATE_NAME}:
            continue
        if replaced and source.name in _artifact_names(replaced):
            continue
        target = stage / source.name
        if target.exists():
            continue
        if path.parent.name == "generations":
            try:
                os.link(source, target)
            except OSError as error:
                # A root-created migration can leave immutable 0644 files
                # owned by root. Linux protected_hardlinks correctly prevents
                # the unprivileged worker from linking those inodes. Copying
                # readable bytes into the worker's own stage preserves that
                # protection and all original inode ownership/permissions.
                if error.errno not in {errno.EPERM, errno.EACCES, errno.EXDEV,
                                       errno.EMLINK, errno.ENOTSUP}:
                    raise
                shutil.copy2(source, target)
        else:
            # Mutable legacy evidence must not share writable inodes with a
            # canonical immutable generation.
            shutil.copy2(source, target)


def _finish_generation(root, config, stage, report, previous, evaluator_config):
    evaluator = _gate_module()
    raw = _json_bytes(report)
    gate = evaluator.evaluate(evaluator_config, report, previous,
                              report_sha256=hashlib.sha256(raw).hexdigest())
    _write(stage / "comparison.json", raw)
    _write(stage / GATE_NAME, _json_bytes(gate))
    _write(stage / "catalog.json", _json_bytes({"schema_version": 2,
        "generated_at": report["generated_at"], "streams": config.get("streams", [])}))
    lines = ["# Object-store archive verification", "", f"Generated: {report['generated_at']}", ""]
    lines.extend(f"- {name}: {value.get('verification_id', value.get('verified_at', 'unknown'))}" for name, value in report.get("jobs", {}).items())
    _write(stage / "comparison.md", ("\n".join(lines) + "\n").encode())
    _write(stage / ".pin.lock", b"")
    # Privileged gate refreshes and unprivileged collection publish into the
    # same tree. Evidence is readable by both; it contains no credentials.
    stage.chmod(0o755)
    for path in stage.iterdir():
        if path.is_file():
            if path.stat().st_nlink == 1:
                path.chmod(0o644)
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    _fsync_dir(stage)
    generation = root / "generations" / uuid.uuid4().hex
    stage.rename(generation)
    _fsync_dir(generation.parent)
    legacy = root / "latest"
    if legacy.is_dir() and not legacy.is_symlink() and previous:
        # Preserve the exact pre-migration gate alongside the unmodified
        # legacy report so rollback restores a hash-bound pair.
        _write(legacy / GATE_NAME, _json_bytes(previous))
        _fsync_dir(legacy)
    _switch(root, generation)
    try:
        _compatibility_gate(config, gate)
    except PermissionError:
        # The unprivileged worker has committed authoritative report+gate.
        # Its privileged ExecStopPost refreshes the legacy compatibility file.
        pass
    return report, gate


def _generations_root(root):
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o755)
    generations = root / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    generations.chmod(0o755)
    if os.geteuid() == 0:
        owner = root.stat()
        os.chown(generations, owner.st_uid, owner.st_gid)
    return generations


@contextmanager
def _publication_storage(config, *, values=None, artifacts_dir=None, job_name=None):
    """Serialize local growth with page writers, preserving the disk reserve.

    Canonical generations are outside the recovery cache quota, but consume
    the same filesystem. Check their temporary allocations separately while
    holding the common resource lock. Lock order is always storage then commit.
    """
    try:
        from aurora_object_store_s3 import resource_lock, InventoryError
    except ModuleNotFoundError:
        spec = importlib.util.spec_from_file_location("aurora_storage_guard", Path(__file__).with_name("aurora_object_store_s3.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        resource_lock, InventoryError = module.resource_lock, module.InventoryError
    with resource_lock(config):
        root = Path(config["manifest_root"])
        latest = root / "latest"
        extra = 1024 * 1024 + (4 * len(_json_bytes(values)) if values is not None else 0)
        for filename in ("comparison.json", GATE_NAME, "catalog.json"):
            path = latest / filename
            if path.is_file():
                extra += 4 * path.stat().st_size
        if artifacts_dir is not None:
            extra += sum((Path(artifacts_dir) / name).stat().st_size for name in _artifact_names(job_name)
                         if (Path(artifacts_dir) / name).is_file())
        if latest.is_dir():
            # Reserve space for every carried artifact: even immutable files
            # may need isolated copies under protected_hardlinks/cross-UID or
            # filesystem restrictions. Successful links simply use less space.
            excluded={"comparison.json", "comparison.md", "catalog.json", GATE_NAME}
            if job_name is not None:
                excluded.update(_artifact_names(job_name))
            extra += sum(path.stat().st_size for path in latest.iterdir()
                         if path.is_file() and not path.name.startswith('.') and path.name not in excluded)
        disk = root
        while not disk.exists():
            disk = disk.parent
        reserve = int(config.get("recovery_free_reserve_bytes", 50 * 1024**3))
        if shutil.disk_usage(disk).free - extra < reserve:
            raise InventoryError("Canonical evidence publication would cross the filesystem free-space reserve", "resource")
        yield


def publish_family(config, job_name, values, artifacts_dir, *, validate_epoch=None):
    with _publication_storage(config, values=values, artifacts_dir=artifacts_dir, job_name=job_name):
        return _publish_family(config, job_name, values, artifacts_dir, validate_epoch=validate_epoch)


def _publish_family(config, job_name, values, artifacts_dir, *, validate_epoch=None):
    """Merge a finished family at commit time and return (report, gate).

    validate_epoch() executes under .inventory.lock and must raise if the
    queue generation has been invalidated. No stale family can then publish.
    """
    names = {job["name"] for job in config["jobs"]}
    if job_name not in names or not re.fullmatch(r"[A-Za-z0-9_-]+", job_name):
        raise ValueError("unconfigured or unsafe archive family")
    for field in ("verification_id", "evidence_started_at", "verification_completed_at"):
        if not isinstance(values.get(field), str) or not values[field]:
            raise ValueError(f"family evidence requires {field}")
    root = Path(config["manifest_root"])
    generations = _generations_root(root)
    stage = Path(tempfile.mkdtemp(prefix=".incoming-", dir=generations))
    evaluator = _gate_module()
    inputs = evaluator.load_evaluation_inputs(config)
    try:
        for filename in _artifact_names(job_name):
            source = Path(artifacts_dir) / filename
            if not source.exists():
                continue
            if source.is_symlink() or not source.is_file():
                raise ValueError("family artifacts must be regular files")
            shutil.copy2(source, stage / source.name)
        for suffix in ("local.tsv", "s3.tsv"):
            if not (stage / f"{job_name}-{suffix}").is_file():
                raise ValueError(f"family artifact missing: {job_name}-{suffix}")
        with _commit_lock(root):
            if validate_epoch is not None:
                validate_epoch()
            path, report, previous, digest = _load_previous(root, config)
            if report.get("jobs", {}).get(job_name, {}).get("verification_id") == values["verification_id"]:
                # A crash after pointer switch but before the queue commit is
                # idempotent: do not manufacture another observation.
                return report, previous
            _merge_artifacts(path, stage, job_name)
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            merged = {**report.get("jobs", {}), job_name: dict(values)}
            merged[job_name]["verified_at"] = values["evidence_started_at"]
            result = {"schema_version": 6, "generated_at": now,
                "verification_mode": "incremental" if report else "full",
                "verified_jobs": [job_name], "jobs": merged,
                "evidence_floor_generated_at": min(
                    (x.get("evidence_started_at", x.get("verified_at", now)) for x in merged.values()), key=evaluator.parse_time)}
            if report:
                result.update(base_generated_at=report["generated_at"], base_report_sha256=digest,
                              incremental_depth=int(report.get("incremental_depth", 0)) + 1)
                inputs["_incremental_base_trusted"] = True
            return _finish_generation(root, config, stage, result, previous, inputs)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def refresh_gate(config):
    root = Path(config["manifest_root"])
    if (root / "latest").is_symlink() or config.get("recovery_enabled", False):
        with _publication_storage(config):
            return _refresh_gate(config)
    return _refresh_gate(config)


def _refresh_gate(config):
    """Reevaluate time-dependent readiness even when report bytes are unchanged."""
    root = Path(config["manifest_root"])
    evaluator = _gate_module()
    inputs = evaluator.load_evaluation_inputs(config)
    with _commit_lock(root):
        path, report, previous, digest = _load_previous(root, config)
        if path is None:
            raise FileNotFoundError("canonical archive comparison is missing")
        inputs.update(evaluator.load_evaluation_inputs(config, report, previous))
        if not (root / "latest").is_symlink() and not config.get("recovery_enabled", False):
            state = evaluator.evaluate(inputs, report, previous, report_sha256=digest)
            _compatibility_gate(config, state)
            return state
        generations = _generations_root(root)
        stage = Path(tempfile.mkdtemp(prefix=".incoming-", dir=generations))
        try:
            _merge_artifacts(path, stage, None)
            _, state = _finish_generation(root, config, stage, report, previous, inputs)
            return state
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def cleanup_generations(config):
    """Bound history while preserving current and actively pinned generations."""
    root = Path(config["manifest_root"])
    removed = []
    with _commit_lock(root):
        current = (root / "latest").resolve()
        generations = root / "generations"
        paths = sorted((p for p in generations.iterdir() if re.fullmatch(r"[0-9a-f]{32}", p.name)),
                       key=lambda p: p.stat().st_mtime, reverse=True) if generations.exists() else []
        for path in paths[max(2, int(config.get("history_keep", 12))):]:
            if path == current:
                continue
            with (path / ".pin.lock").open("rb") as pin:
                try:
                    fcntl.flock(pin, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                shutil.rmtree(path)
                removed.append(path.name)
        # A worker's hard service bound is thirteen hours. Staging trees left
        # by a process crash cannot still belong to a live worker after fifteen
        # hours. Never follow a link or touch a recent candidate directory.
        cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - 15 * 3600
        if generations.exists():
            for path in generations.iterdir():
                if (re.fullmatch(r"\.incoming-[A-Za-z0-9_-]{8}", path.name)
                        and not path.is_symlink() and path.is_dir()
                        and path.stat().st_mtime < cutoff):
                    shutil.rmtree(path)
                    removed.append(path.name)
    return removed


def rollback_legacy(config, legacy_path):
    """Restore a retained legacy tree atomically at a stopped-worker boundary."""
    root = Path(config["manifest_root"]).resolve()
    legacy = Path(legacy_path).resolve()
    if legacy.parent != root or not legacy.name.startswith("legacy-latest-") or not legacy.is_dir():
        raise ValueError("rollback requires a retained legacy-latest directory")
    with _commit_lock(root):
        if not (root / "latest").is_symlink():
            raise ValueError("current evidence is not a generation pointer")
        _exchange(legacy, root / "latest")
        _fsync_dir(root)
        gate_path = root / "latest" / GATE_NAME
        if gate_path.is_file():
            _compatibility_gate(config, json.loads(gate_path.read_bytes()))
