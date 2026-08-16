#!/usr/bin/env python3
"""Start retention promptly, but only when verification is stably clean."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Callable


DEFAULT_STATE = Path(
    "/var/lib/aurora-cloud/object-store-verification-gate/state.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--unit", default="aurora-ass-retention.service")
    return parser.parse_args()


def trigger(
    state_path: Path,
    unit: str,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    raw_ready = state.get("raw_retention_ready")
    if raw_ready is None:
        raw_ready = state.get("clean") and state.get("stable_parity")
    if not raw_ready:
        print("Retention remains paused: raw archive verification is not stably clean.")
        return 0
    completed = run(
        ["/bin/systemctl", "--no-block", "start", unit],
        check=False,
    )
    return int(completed.returncode)


def main() -> int:
    args = parse_args()
    return trigger(args.state, args.unit)


if __name__ == "__main__":
    raise SystemExit(main())
