#!/usr/bin/env python3
"""Path-aware HATPRO discovery, using the existing rsync and archive writers."""

import argparse
from decimal import Decimal, InvalidOperation
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import stat
import subprocess
import tempfile
import time


HEADER = b"AURORA_HATPRO_DISCOVERY_1\0"
TRAILER = b"AURORA_HATPRO_DISCOVERY_END\0"
REMOTE_FIND = r'''set -euo pipefail
cd "$1"
printf 'AURORA_HATPRO_DISCOVERY_1\0'
LC_ALL=C find . -type f -name "$2" -printf '%P\0%s\0%T@\0'
printf 'AURORA_HATPRO_DISCOVERY_END\0'
'''


def path_check(value, pattern):
    if (not isinstance(value, str) or not value or "\0" in value
            or value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/"))
            or not fnmatch.fnmatchcase(PurePosixPath(value).name, pattern)):
        raise ValueError("Invalid HATPRO relative path")
    return value


def metadata(size, mtime):
    if isinstance(size, bool) or not str(size).isdigit():
        raise ValueError("Invalid source size")
    try:
        timestamp = Decimal(str(mtime))
    except InvalidOperation as exc:
        raise ValueError("Invalid source modification time") from exc
    if not timestamp.is_finite() or timestamp <= 0:
        raise ValueError("Invalid source modification time")
    return {"size": int(size), "mtime": str(timestamp)}


def parse_inventory(data, pattern):
    if not data.startswith(HEADER) or not data.endswith(TRAILER):
        raise ValueError("Incomplete HATPRO inventory; refusing empty discovery")
    body = data[len(HEADER):-len(TRAILER)]
    if not body:
        return {}
    fields = body.split(b"\0")
    if fields.pop() != b"" or len(fields) % 3:
        raise ValueError("Malformed HATPRO inventory")
    rows = {}
    for index in range(0, len(fields), 3):
        name = path_check(os.fsdecode(fields[index]), pattern)
        if name in rows:
            raise ValueError("Duplicate source path")
        rows[name] = metadata(fields[index + 1].decode("ascii"), fields[index + 2].decode("ascii"))
    return rows


def local_matches(root, name, expected):
    """Never follow a destination symlink, including intermediate directories."""
    current = root
    parts = name.split("/")
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        if index < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Unsafe destination directory: " + str(current))
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError("Unsafe destination file: " + str(current))
    return (info.st_size == expected["size"]
            and info.st_mtime_ns // 1_000_000_000 == int(Decimal(expected["mtime"])))


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".hatpro-discovery-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_records(path, binding, rows):
    atomic_write(path, (json.dumps({"schema": 1, "binding": binding, "files": rows},
                                 sort_keys=True) + "\n").encode())


def load_records(path, binding, pattern):
    if not path.exists():
        return None
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Discovery checkpoint must be a regular file")
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get("schema") != 1
            or value.get("binding") != binding or not isinstance(value.get("files"), dict)):
        raise ValueError("Discovery checkpoint configuration mismatch or corruption")
    rows = {}
    for name, row in value["files"].items():
        if not isinstance(row, dict) or set(row) != {"size", "mtime"}:
            raise ValueError("Malformed discovery checkpoint metadata")
        rows[path_check(name, pattern)] = metadata(row["size"], row["mtime"])
    return rows


def choose(rows, pending, ignored, destination, cursor, until):
    transfer, receipts, deferred = {}, {}, {}
    retired = 0
    for name, row in rows.items():
        stamp = Decimal(row["mtime"])
        if stamp > until:
            if name in pending:
                deferred[name] = pending[name]
            continue
        matches = local_matches(destination, name, row)
        # An explicit fresh-start baseline may omit old history, but never a
        # new relative path or changed metadata introduced after that baseline.
        if name not in pending and ignored.get(name) == row and stamp <= cursor:
            continue
        if name in pending or stamp > cursor or not matches:
            transfer[name] = row
            receipts[name] = row
    for name, row in pending.items():
        if name in rows:
            continue
        # A source path may move between attempts. Keep completed cloud handoffs,
        # but never advertise a missing/partial obsolete path as a copied file.
        if local_matches(destination, name, row):
            receipts[name] = row
        else:
            retired += 1
    return transfer, receipts, deferred, retired


def nul_paths(rows):
    return b"".join(os.fsencode(name) + b"\0" for name in sorted(rows))


def run(config, *, execute=subprocess.run, clock=time.time):
    destination = Path(config.destination)
    state = Path(config.state_file)
    state.parent.mkdir(parents=True, exist_ok=True)
    # Share the existing backfill lock without changing its program or timer.
    with (state.parent / "hatpro-backfill.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("HATPRO discovery deferred: sync/backfill already running.")
            return
        destination.mkdir(parents=True, exist_ok=True)
        if not stat.S_ISDIR(destination.lstat().st_mode):
            raise ValueError("Expected a real HATPRO destination directory")
        known_hosts = Path(config.known_hosts)
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        pending_path = Path(str(state) + ".pending.json")
        baseline_path = Path(str(state) + ".baseline.json")
        identity = {key: getattr(config, key) for key in (
            "source_user", "source_host", "source_port", "source_path", "source_pattern",
            "destination", "source_auth", "ssh_key", "start_fresh")}
        binding = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        pending = load_records(pending_path, binding, config.source_pattern) or {}
        baseline = load_records(baseline_path, binding, config.source_pattern)
        new_state = not state.exists()
        if not new_state and not stat.S_ISREG(state.lstat().st_mode):
            raise ValueError("Cursor must be a regular file")
        cursor_text = state.read_text().strip() if not new_state else "0"
        if not cursor_text.isdigit():
            raise ValueError("Invalid HATPRO cursor; refusing to reset it")
        cursor = int(cursor_text)
        until = int(clock())
        if until < cursor:
            raise ValueError("Clock is behind the saved HATPRO cursor")
        options = ["-p", str(config.source_port), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                   "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=" + str(known_hosts)]
        if config.source_auth == "ssh_key":
            if not Path(config.ssh_key).is_file():
                raise ValueError("Missing HATPRO source SSH key")
            options += ["-i", config.ssh_key]
        else:
            options += ["-o", "IdentityFile=none", "-o", "PubkeyAuthentication=no"]
        target = config.source_user + "@" + config.source_host
        remote = "bash -s -- " + shlex.quote(config.source_path) + " " + shlex.quote(config.source_pattern)
        result = execute(["ssh", *options, target, remote], input=REMOTE_FIND.encode(),
                         capture_output=True, check=True)
        rows = parse_inventory(result.stdout, config.source_pattern)
        if baseline is None:
            threshold = until if new_state else cursor
            baseline = {name: row for name, row in rows.items()
                        if config.start_fresh and Decimal(row["mtime"]) <= threshold}
            save_records(baseline_path, binding, baseline)
        if new_state and config.start_fresh and not pending:
            atomic_write(state, (str(until) + "\n").encode())
            print("Initialized fresh HATPRO path baseline; historical files not pulled.")
            return
        transfer, receipts, deferred, retired = choose(
            rows, pending, baseline, destination, cursor, until)
        # Persist before copying; failed transfer/dispatch cannot strand an old
        # timestamp behind the cursor once its cloud path starts to exist.
        save_records(pending_path, binding, {**receipts, **deferred})
        if receipts:
            if not os.access(config.dispatcher, os.X_OK):
                raise ValueError("Archive dispatcher unavailable; pending handoffs retained")
            with tempfile.TemporaryDirectory(prefix="hatpro-discovery-", dir=state.parent) as temporary:
                files_from = Path(temporary) / "files"
                if transfer:
                    files_from.write_bytes(nul_paths(transfer))
                    environment = os.environ.copy()
                    environment["RSYNC_RSH"] = shlex.join(["ssh", *options])
                    # Preserve the commissioned copy implementation and flags.
                    execute(["rsync", "-a", "--partial", "--from0", "--files-from=" + str(files_from),
                             target + ":" + config.source_path.rstrip("/") + "/",
                             str(destination) + "/"], env=environment, check=True)
                files_from.write_bytes(nul_paths(receipts))
                execute([config.dispatcher, "enqueue", "--job", "raw", "--base", str(destination),
                         "--files-from", str(files_from), "--null"], check=True)
        atomic_write(state, (str(until) + "\n").encode())
        # Clearing follows durable enqueue. Repeating an enqueue after a crash
        # is safe; losing it before cursor advancement is not.
        if deferred:
            save_records(pending_path, binding, deferred)
        else:
            pending_path.unlink()
            sync_directory(state.parent)
        print(f"HATPRO discovery: scanned={len(rows)} selected={len(transfer)} "
              f"enqueued={len(receipts)} deferred={len(deferred)} retired_source_paths={retired}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-user", "source-host", "source-path", "source-pattern", "destination",
                 "state-file", "ssh-key", "known-hosts"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--source-port", type=int, required=True)
    parser.add_argument("--source-auth", choices=("tailscale", "ssh_key"), required=True)
    parser.add_argument("--start-fresh", action="store_true")
    parser.add_argument("--dispatcher", default="/usr/local/bin/aurora-archive-dispatch")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
