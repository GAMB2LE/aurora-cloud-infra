#!/usr/bin/env python3
"""Switch only BLE environment keys, retaining a reversible original binding."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid


KEYS = ("BLESENSORS_DIR", "BLESENSORS_ZARR_PATH", "BLESENSORS_SOURCE_LABEL")


def bindings(text: str) -> dict[str, list[str]]:
    return {key: [line if line.endswith("\n") else line + "\n"
                  for line in text.splitlines(keepends=True) if line.startswith(key + "=")]
            for key in KEYS}


def replace_bindings(text: str, replacement: dict[str, list[str]]) -> str:
    other = "".join(line for line in text.splitlines(keepends=True)
                    if not any(line.startswith(key + "=") for key in KEYS))
    values = "".join(line.rstrip("\n") + "\n" for key in KEYS for line in replacement[key])
    if values and other and not other.endswith("\n"):
        other += "\n"
    return other + values


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    info = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=".ble-env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode) if info else mode)
        if info:
            os.chown(temporary, info.st_uid, info.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure(env_file: Path, state_file: Path, action: str, values: dict, check: bool) -> dict:
    if env_file.is_symlink() or not env_file.is_file() or state_file.is_symlink():
        raise ValueError("Require a regular existing environment file and non-symlink state")
    if action not in ("install", "rollback"):
        raise ValueError("Unknown continuity environment action")
    if set(values) != set(KEYS) or any(not isinstance(v, str) or any(c in v for c in "\r\n\x00") for v in values.values()):
        raise ValueError("Expected exactly the three single-line BLE environment values")
    text = env_file.read_text(encoding="utf-8")
    current = bindings(text)
    previous = json.loads(state_file.read_text()) if state_file.exists() else None
    selected = {key: [key + "=" + values[key] + "\n"] for key in KEYS}
    if action == "install":
        if previous and previous["active"]:
            if current not in (previous["selected"], previous["original"]):
                raise ValueError("BLE environment changed since activation; review before replacing it")
            original = previous["original"]
        else:
            original = current
        next_state = {"schema_version": 1, "active": True, "original": original, "selected": selected}
        target = selected
    else:
        if previous is None:
            raise ValueError("No saved BLE environment baseline exists for rollback")
        if current not in (previous["selected"], previous["original"]):
            raise ValueError("BLE environment changed after activation; rollback will not overwrite it")
        target = previous["original"]
        next_state = dict(previous, active=False)
    # Preserve the whole file byte-for-byte when the selected binding is already
    # correct. Changes to unrelated settings never prevent BLE-only rollback.
    updated = text if current == target else replace_bindings(text, target)
    changed = updated != text or previous != next_state
    result = {"changed": changed, "action": action, "check": check, "state": str(state_file), "keys": list(KEYS)}
    if check or not changed:
        return result
    state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    backup = state_file.parent / "backups" / stamp
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(env_file, backup / env_file.name)
    if state_file.exists():
        shutil.copy2(state_file, backup / state_file.name)
    # Save the baseline before publishing the environment, allowing an interrupted
    # activation to be retried or rolled back without losing the original values.
    atomic_write(state_file, json.dumps(next_state, indent=2) + "\n")
    if updated != text:
        atomic_write(env_file, updated)
    result["backup"] = str(backup)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("install", "rollback"), required=True)
    parser.add_argument("--env", type=Path, default=Path("/etc/aurora-dashboard.env"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print(json.dumps(configure(args.env, args.state, args.action,
                               json.loads(os.environ["BLESENSORS_CONTINUITY_VALUES"]), args.check)))
