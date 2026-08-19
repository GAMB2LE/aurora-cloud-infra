#!/usr/bin/env python3
"""Refresh only object-store catalogue families repaired successfully.

The latest full report remains the evidence base. A successful exact repair
publishes a small result document; this worker selects only jobs that copied at
least one settled path and runs the existing bounded incremental inventory.
Transient inventory failures receive one delayed retry. The inventory's global
lock and per-family evidence timestamps remain authoritative.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
from typing import Callable


DEFAULT_REPAIR_RESULT = Path(
    "/var/lib/aurora-cloud/object-store-repair/result.json"
)
DEFAULT_REPORT = Path(
    "/data/aurora/internal/object_store_manifests/latest/comparison.json"
)
DEFAULT_INVENTORY = Path("/usr/local/bin/aurora-object-store-inventory")
DEFAULT_STATE = Path(
    "/var/lib/aurora-cloud/object-store-repair/recheck-state.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repair-result", type=Path, default=DEFAULT_REPAIR_RESULT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--retry-delay-seconds", type=int, default=600)
    parser.add_argument("--confirmations", type=int, default=2)
    parser.add_argument("--confirmation-delay-seconds", type=int, default=600)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    return parser.parse_args()


def repaired_jobs(result: dict, report: dict) -> list[str]:
    if result.get("report") != report.get("generated_at"):
        return []
    configured = set(report.get("jobs", {}))
    return sorted(
        str(item["job"])
        for item in result.get("jobs", [])
        if item.get("job") in configured
        and int(item.get("ready", 0)) > 0
        and int(item.get("returncode", 1)) == 0
    )


def recheck(
    *,
    result_path: Path,
    report_path: Path,
    inventory: Path,
    attempts: int,
    retry_delay_seconds: int,
    confirmations: int = 1,
    confirmation_delay_seconds: int = 600,
    state_path: Path | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    result = json.loads(result_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    jobs = repaired_jobs(result, report)
    if not jobs:
        print("No successfully repaired catalogue families require rechecking.")
        return 0

    repair_report = str(result.get("report") or "")
    if state_path is not None and state_path.exists():
        previous = json.loads(state_path.read_text(encoding="utf-8"))
        if (
            previous.get("repair_report") == repair_report
            and previous.get("jobs") == jobs
            and int(previous.get("confirmations", 0)) >= max(1, confirmations)
        ):
            print("This repair report has already been confirmed.")
            return 0

    command = [str(inventory), "--reuse-latest"]
    for name in jobs:
        command.extend(("--job", name))

    maximum_attempts = max(1, attempts)
    required_confirmations = max(1, confirmations)
    for confirmation in range(1, required_confirmations + 1):
        for attempt in range(1, maximum_attempts + 1):
            completed = run(command, check=False)
            if completed.returncode == 0:
                break
            if attempt < maximum_attempts:
                print(
                    f"Post-repair verification attempt {attempt} failed; "
                    f"retrying in {retry_delay_seconds} seconds."
                )
                sleep(max(0, retry_delay_seconds))
        if completed.returncode != 0:
            return int(completed.returncode or 1)
        print(
            f"Post-repair confirmation {confirmation} of "
            f"{required_confirmations} completed for: " + ", ".join(jobs)
        )
        if confirmation < required_confirmations:
            sleep(max(0, confirmation_delay_seconds))

    if state_path is not None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "repair_report": repair_report,
                    "jobs": jobs,
                    "confirmations": required_confirmations,
                    "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(state_path)
    return 0


def main() -> int:
    args = parse_args()
    return recheck(
        result_path=args.repair_result,
        report_path=args.report,
        inventory=args.inventory,
        attempts=args.attempts,
        retry_delay_seconds=args.retry_delay_seconds,
        confirmations=args.confirmations,
        confirmation_delay_seconds=args.confirmation_delay_seconds,
        state_path=args.state,
    )


if __name__ == "__main__":
    raise SystemExit(main())
