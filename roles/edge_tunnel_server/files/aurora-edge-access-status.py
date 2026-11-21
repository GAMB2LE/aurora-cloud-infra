#!/usr/bin/env python3
"""Check recovery transports without logging in or opening a desktop session."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import sys


ENDPOINTS = {
    "ass-linux": ("ssh", 2201),
    "aps-linux": ("ssh", 2202),
    "ass-proxmox": ("ssh", 2211),
    "aps-proxmox": ("ssh", 2212),
    "ass-nx": ("nx", 2401),
    "aps-nx": ("nx", 2402),
}


def validate_config(config: dict) -> list[dict]:
    if config.get("version") != 1 or not isinstance(config.get("endpoints"), list):
        raise ValueError("expected version 1 and an endpoints list")
    endpoints = config["endpoints"]
    if len(endpoints) != len(ENDPOINTS):
        raise ValueError("exactly four SSH and two NX endpoints are required")
    seen = set()
    for endpoint in endpoints:
        name = endpoint.get("name")
        if name in seen or name not in ENDPOINTS:
            raise ValueError("unknown or duplicate endpoint name")
        seen.add(name)
        if (endpoint.get("kind"), endpoint.get("port")) != ENDPOINTS[name]:
            raise ValueError(f"unexpected endpoint kind or port for {name}")
        fingerprint = endpoint.get("expected_fingerprint", "")
        if not isinstance(fingerprint, str) or (
            fingerprint and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", fingerprint)
        ):
            raise ValueError(f"invalid SHA256 fingerprint for {name}")
    return endpoints


def fingerprints_from_keyscan(output: str) -> list[str]:
    fingerprints = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != "ssh-ed25519":
            continue
        try:
            key = base64.b64decode(parts[2], validate=True)
        except ValueError:
            continue
        # ED25519 SSH wire keys contain two length-prefixed strings: the
        # algorithm name and the 32-byte public key. Reject malformed output.
        if len(key) != 51 or key[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
            continue
        digest = base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip("=")
        fingerprints.add("SHA256:" + digest)
    return sorted(fingerprints)


def probe_endpoint(endpoint: dict, timeout: float = 3.0) -> dict:
    result = {
        "name": endpoint["name"],
        "kind": endpoint["kind"],
        "host": "127.0.0.1",
        "port": endpoint["port"],
        "transport": "unreachable",
        "authentication": "not_tested",
    }
    if endpoint["kind"] == "nx":
        result["desktop"] = "not_tested"
    else:
        result["identity"] = "not_checked"
        result["expected_fingerprint"] = endpoint.get("expected_fingerprint", "")
    try:
        with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=timeout) as connection:
            if endpoint["kind"] == "nx":
                # The SSH relay accepts TCP before it attempts the downstream
                # connection. Observe an immediate EOF instead of counting a
                # refused downstream port as a working NoMachine endpoint.
                connection.settimeout(timeout)
                try:
                    greeting = connection.recv(1)
                    result["transport"] = "tcp_received_data" if greeting else "closed_after_connect"
                except socket.timeout:
                    result["transport"] = "tcp_open_no_greeting"
                return result
        result["transport"] = "tcp_connected"
    except OSError as exc:
        result["error"] = type(exc).__name__
        return result
    try:
        scan = subprocess.run(
            ["ssh-keyscan", "-T", str(max(1, int(timeout))), "-t", "ed25519",
             "-p", str(endpoint["port"]), "127.0.0.1"],
            capture_output=True, text=True, timeout=timeout + 2, check=False,
        )
        fingerprints = fingerprints_from_keyscan(scan.stdout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["identity"] = "probe_failed"
        result["error"] = type(exc).__name__
        return result
    result["observed_fingerprints"] = fingerprints
    if not fingerprints:
        result["identity"] = "probe_failed"
        return result
    result["transport"] = "ssh_handshake"
    expected = result["expected_fingerprint"]
    if not expected:
        result["identity"] = "unconfigured"
    elif fingerprints == [expected]:
        result["identity"] = "verified"
    else:
        result["identity"] = "mismatch"
    return result


def report(endpoints: list[dict], timeout: float) -> dict:
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda endpoint: probe_endpoint(endpoint, timeout), endpoints))
    ssh_verified = all(item["identity"] == "verified" for item in results if item["kind"] == "ssh")
    nx_connected = all(item["transport"] in {"tcp_received_data", "tcp_open_no_greeting"}
                       for item in results if item["kind"] == "nx")
    return {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "transport_verified" if ssh_verified and nx_connected else "degraded",
        "ssh_transport_status": "verified" if ssh_verified else "degraded",
        "nx_tcp_status": "open_application_unverified" if nx_connected else "degraded",
        "authentication": "not_tested",
        "desktop": "not_tested",
        "endpoints": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/aurora-edge-access-status.json")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--compact", action="store_true", help="emit one JSON line for the journal")
    args = parser.parse_args(argv)
    try:
        if not 0 < args.timeout <= 10:
            raise ValueError("timeout must be greater than zero and at most 10 seconds")
        endpoints = validate_config(json.loads(Path(args.config).read_text()))
        result = report(endpoints, args.timeout)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(json.dumps({"status": "configuration_error", "error": str(exc)}))
        return 2
    print(json.dumps(result, indent=None if args.compact else 2))
    return 0 if result["status"] == "transport_verified" else 1


if __name__ == "__main__":
    sys.exit(main())
