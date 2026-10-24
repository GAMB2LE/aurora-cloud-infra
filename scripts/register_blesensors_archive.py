#!/usr/bin/env python3
"""Add only BLE archive bindings, retaining deployed code and other settings."""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import uuid


BLE_PRODUCT_EXCLUDES = [
    "blesensors/.blesensors.zarr.staging.tmp/**",
    "blesensors/.blesensors.zarr.previous.tmp/**",
    "blesensors/blesensors.ingest.json",
    "blesensors/blesensors.ingest.tmp",
    "blesensors/blesensors.lock",
]


def patch_product_job(data: dict, product_root: str) -> dict:
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("Missing deployed object-store jobs list")
    matches = [i for i, job in enumerate(jobs) if isinstance(job, dict) and job.get("name") == "products"]
    if len(matches) != 1:
        raise ValueError("Expected exactly one deployed products archive job")
    index = matches[0]
    job = jobs[index]
    if job.get("source", "").rstrip("/") != product_root.rstrip("/"):
        raise ValueError("Deployed products archive source does not match the BLE product parent")
    exclude = job.get("exclude", [])
    if not isinstance(exclude, list) or any(not isinstance(x, str) for x in exclude):
        raise ValueError("Unrecognised products exclusion list")
    result = list(jobs)
    result[index] = dict(job, exclude=exclude + [value for value in BLE_PRODUCT_EXCLUDES if value not in exclude])
    return dict(data, jobs=result)


def patch_product_wrapper(source: str, kind: str, product_root: str) -> str:
    job_variable, source_variable, array = {
        "gws": ("job_name", "source_dir", "rsync_opts"),
        "object": ("job", "source_path", "base_args"),
    }[kind]
    for variable, expected in ((job_variable, "products"), (source_variable, product_root.rstrip("/"))):
        matches = re.findall(r"^" + variable + r"=(.+)$", source, re.M)
        if len(matches) != 1:
            raise ValueError(f"Unrecognised deployed {kind} {variable} binding")
        words = shlex.split(matches[0])
        if len(words) != 1 or words[0].rstrip("/") != expected:
            raise ValueError(f"Unexpected deployed {kind} {variable} binding")
    expression = re.compile(r"^" + array + r"=\(\n(.*?)^\)\n", re.M | re.S)
    matches = list(expression.finditer(source))
    if len(matches) != 1 or '"${' + array + '[@]}"' not in source:
        raise ValueError(f"Unrecognised deployed {kind} option array/invocation")
    missing = [pattern for pattern in BLE_PRODUCT_EXCLUDES
               if "--exclude=" + shlex.quote(pattern) not in source
               and "--exclude " + shlex.quote(pattern) not in source]
    if not missing:
        return source
    end = matches[0].end() - 2  # Immediately before the array's closing ')\n'.
    result = source[:end] + "".join("  --exclude " + shlex.quote(pattern) + "\n" for pattern in missing) + source[end:]
    subprocess.run(["/bin/bash", "-n"], input=result, text=True, check=True, capture_output=True)
    return result


def require_idle_product_workers() -> None:
    units = ["aurora-gws-rsync-products.service", "aurora-object-store-copy-products.service",
             "aurora-object-store-recovery-worker@products.service",
             "aurora-object-store-inventory-incremental@products.service"]
    result = subprocess.run(["/bin/systemctl", "show", *units,
                             "--property=Id,LoadState,ActiveState,Job"],
                            capture_output=True, text=True, check=True)
    blocks = [dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
              for block in result.stdout.strip().split("\n\n")]
    if len(blocks) != len(units):
        raise ValueError("Could not establish product archive worker state")
    for block in blocks:
        if block.get("LoadState") == "not-found":
            continue
        if block.get("ActiveState") not in ("inactive", "failed") or block.get("Job") not in ("", "0"):
            raise ValueError("Product archive worker is active; retry BLE registration at an idle boundary: " + block.get("Id", "unknown"))


def add_stream(streams: list, stream: dict) -> list:
    if not isinstance(streams, list) or any(not isinstance(x, dict) for x in streams):
        raise ValueError("Expected an existing list of archive streams")
    matches = [i for i, value in enumerate(streams) if value.get("name") == "blesensors"]
    if len(matches) > 1:
        raise ValueError("Multiple deployed BLE bindings require manual review")
    result = list(streams)
    if matches:
        result[matches[0]] = stream
    else:
        result.append(stream)
    return result


def patch_embedded_json(source: str, variable: str, stream: dict) -> str:
    tree = ast.parse(source)
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == variable
                           for target in node.targets)]
    if len(assignments) != 1:
        raise ValueError(f"Expected exactly one {variable} assignment")
    call = assignments[0].value
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name) and call.func.value.id == "json"
            and call.func.attr == "loads" and len(call.args) == 1
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)):
        raise ValueError(f"Unrecognised deployed {variable} JSON binding")
    value = call.args[0]
    data = json.loads(value.value)
    if variable == "CONFIG":
        updated = dict(data, streams=add_stream(data["streams"], stream))
    else:
        updated = add_stream(data, stream)
    if updated == data:
        return source
    # AST columns are UTF-8 byte offsets, including when a deployed comment
    # contains non-ASCII characters. Replace only the JSON string argument.
    lines = source.encode("utf-8").splitlines(keepends=True)
    start = sum(map(len, lines[:value.lineno - 1])) + value.col_offset
    end = sum(map(len, lines[:value.end_lineno - 1])) + value.end_col_offset
    replacement = repr(json.dumps(updated, ensure_ascii=True)).encode("utf-8")
    result = (source.encode("utf-8")[:start] + replacement + source.encode("utf-8")[end:]).decode("utf-8")
    compile(result, "archive binding", "exec")
    return result


def register(root: Path, archive_stream: dict, verifier_stream: dict, check: bool,
             product_root: str = "/data/aurora/products") -> dict:
    if archive_stream.get("name") != "blesensors" or archive_stream.get("prune_enabled") is not False:
        raise ValueError("BLE archive registration must keep pruning disabled")
    if verifier_stream.get("name") != "blesensors":
        raise ValueError("Only the BLE verifier stream can be changed")
    bindings = (
        ("usr/local/bin/aurora-mirror-verify", "CONFIG", verifier_stream),
        ("usr/local/bin/aurora-archive-health", "STREAMS", archive_stream),
        ("etc/aurora-object-store/catalog.json", None, archive_stream),
        ("usr/local/bin/aurora-gws-rsync-products", "gws", None),
        ("usr/local/bin/aurora-object-store-copy-products", "object", None),
    )
    changes = []
    for relative, variable, stream in bindings:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing regular deployed archive file: {path}")
        source = path.read_text(encoding="utf-8")
        if variable in ("gws", "object"):
            updated = patch_product_wrapper(source, variable, product_root)
        elif variable:
            updated = patch_embedded_json(source, variable, stream)
        else:
            data = json.loads(source)
            updated_data = patch_product_job(dict(data, streams=add_stream(data["streams"], stream)), product_root)
            updated = source if data == updated_data else json.dumps(updated_data, indent=2) + "\n"
        if updated != source:
            changes.append((path, updated))
    result = {"changed": bool(changes), "check": check, "paths": [str(path) for path, _ in changes]}
    live_root = root.resolve() == Path("/")
    if live_root:
        require_idle_product_workers()
    if check or not changes:
        return result
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    backup = root / "var/backups/aurora-blesensors" / stamp
    backup.mkdir(parents=True, mode=0o700)
    # Validate every target before backing up or replacing any file. Do not
    # restart archive workers or modify their state, evidence, jobs, or timers.
    for path, _ in changes:
        shutil.copy2(path, backup / path.name)
    for path, updated in changes:
        info = path.stat()
        fd, temporary = tempfile.mkstemp(prefix=".blesensors-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(info.st_mode))
            os.chown(temporary, info.st_uid, info.st_gid)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    result["backup"] = str(backup)
    # A worker may have started with the old options between the initial idle
    # check and the replacements. Do not permit writer activation until it exits.
    if live_root:
        require_idle_product_workers()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=Path("/"))
    args = parser.parse_args()
    print(json.dumps(register(args.root, json.loads(os.environ["BLESENSORS_ARCHIVE_STREAM_JSON"]),
                              json.loads(os.environ["BLESENSORS_VERIFIER_STREAM_JSON"]), args.check,
                              os.environ.get("BLESENSORS_PRODUCT_ROOT", "/data/aurora/products"))))
