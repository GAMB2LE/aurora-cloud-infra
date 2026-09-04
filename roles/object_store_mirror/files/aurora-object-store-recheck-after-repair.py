#!/usr/bin/env python3
"""Refresh only object-store catalogue families repaired successfully.

The latest full report remains the evidence base. A successful exact repair
publishes a small result document; this worker selects only jobs that copied at
least one settled path and runs the existing bounded incremental inventory.
Transient inventory failures receive one delayed retry. A subprocess exit of
zero is not itself a clean confirmation: the worker also requires a new,
full-family, gap-free report and waits for the verification gate to record that
exact report. The inventory's global lock and per-family evidence timestamps
remain authoritative.
"""

from __future__ import annotations

import argparse
import hashlib
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
DEFAULT_GATE_STATE = Path(
    "/var/lib/aurora-cloud/object-store-verification-gate/state.json"
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
    parser.add_argument("--gate-state", type=Path, default=DEFAULT_GATE_STATE)
    parser.add_argument("--gate-wait-seconds", type=int, default=120)
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


def confirmation_state_is_trusted(
    state: dict,
    *,
    repair_report: str,
    jobs: list[str],
    required_confirmations: int,
) -> bool:
    try:
        recorded_confirmations = int(state.get("confirmations", 0))
    except (TypeError, ValueError):
        return False
    if (
        state.get("schema_version") != 2
        or state.get("repair_report") != repair_report
        or state.get("jobs") != jobs
        or recorded_confirmations < required_confirmations
    ):
        return False
    reports = state.get("confirmation_reports")
    if not isinstance(reports, list) or len(reports) < required_confirmations:
        return False
    identities: set[tuple[str, str]] = set()
    for item in reports[:required_confirmations]:
        if not isinstance(item, dict):
            return False
        generated_at = item.get("generated_at")
        report_sha256 = item.get("report_sha256")
        if (
            not isinstance(generated_at, str)
            or not generated_at
            or not isinstance(report_sha256, str)
            or len(report_sha256) != 64
        ):
            return False
        try:
            int(report_sha256, 16)
        except ValueError:
            return False
        identities.add((generated_at, report_sha256))
    return len(identities) == required_confirmations


def domain_for_job(name: str) -> str:
    return "raw_retention" if name == "raw" else "products"


def comparison_is_clean(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for field in (
        "missing_from_right",
        "size_mismatch",
        "checksum_mismatch",
    ):
        entries = value.get(field)
        if not isinstance(entries, list) or entries:
            return False
    return True


def load_clean_confirmation(
    report_path: Path,
    jobs: list[str],
    previous_generated_at: str,
) -> tuple[dict, str]:
    raw = report_path.read_bytes()
    report = json.loads(raw)
    generated_at = report.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise RuntimeError("incremental inventory did not publish a timestamp")
    if generated_at == previous_generated_at:
        raise RuntimeError("incremental inventory did not publish a new report")
    verified_jobs = report.get("verified_jobs")
    if not isinstance(verified_jobs, list) or not set(jobs) <= set(verified_jobs):
        raise RuntimeError("incremental report did not verify every repaired family")
    for name in jobs:
        values = report.get("jobs", {}).get(name)
        if not isinstance(values, dict):
            raise RuntimeError(f"incremental report omitted repaired family {name}")
        if not isinstance(values.get("verified_at"), str):
            raise RuntimeError(
                f"incremental report omitted the evidence timestamp for {name}"
            )
        if values.get("verification_scope") != "full_family":
            raise RuntimeError(f"incremental report did not fully verify {name}")
        comparisons = ["source_vs_s3"]
        if name != "raw":
            comparisons.append("source_vs_gws")
        for comparison_name in comparisons:
            if not comparison_is_clean(values.get(comparison_name)):
                raise RuntimeError(
                    f"incremental report kept a settled gap for "
                    f"{name}:{comparison_name}"
                )
    return report, hashlib.sha256(raw).hexdigest()


def wait_for_gate_confirmation(
    *,
    report_path: Path,
    gate_state_path: Path,
    report: dict,
    report_sha256: str,
    jobs: list[str],
    minimum_full_clean: int,
    wait_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    expected_generated_at = report["generated_at"]
    required_domains = {domain_for_job(name) for name in jobs}
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        current_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        if current_sha256 != report_sha256:
            raise RuntimeError(
                "latest inventory changed before its gate observation completed"
            )
        try:
            gate_state = json.loads(gate_state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            gate_state = {}
        if (
            gate_state.get("last_generated_at") == expected_generated_at
            and gate_state.get("report_sha256") == report_sha256
        ):
            domains = gate_state.get("domains")
            if not isinstance(domains, dict):
                raise RuntimeError("verification gate omitted domain state")
            for name in required_domains:
                domain = domains.get(name)
                if not isinstance(domain, dict):
                    raise RuntimeError(
                        f"verification gate omitted the {name} domain"
                    )
                if not domain.get("clean"):
                    raise RuntimeError(
                        f"verification gate rejected the {name} confirmation"
                    )
                if not domain.get("verified_in_report"):
                    raise RuntimeError(
                        f"verification gate did not observe new {name} evidence"
                    )
                if not domain.get("complete_verification"):
                    raise RuntimeError(
                        f"verification gate did not accept complete {name} coverage"
                    )
                if int(domain.get("full_clean_reports_in_streak", 0)) < int(
                    minimum_full_clean
                ):
                    raise RuntimeError(
                        f"verification gate did not record clean {name} "
                        f"confirmation {minimum_full_clean}"
                    )
            return gate_state
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "verification gate did not record the incremental report"
            )
        sleep(1)


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
    gate_state_path: Path = DEFAULT_GATE_STATE,
    gate_wait_seconds: int = 120,
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
    required_confirmations = max(1, confirmations)
    if state_path is not None and state_path.exists():
        previous = json.loads(state_path.read_text(encoding="utf-8"))
        if confirmation_state_is_trusted(
            previous,
            repair_report=repair_report,
            jobs=jobs,
            required_confirmations=required_confirmations,
        ):
            print("This repair report has already been confirmed.")
            return 0

    command = [str(inventory), "--reuse-latest"]
    for name in jobs:
        command.extend(("--job", name))

    maximum_attempts = max(1, attempts)
    previous_generated_at = str(report.get("generated_at") or "")
    confirmation_reports: list[dict[str, str]] = []
    for confirmation in range(1, required_confirmations + 1):
        confirmed_report: dict | None = None
        confirmed_sha256 = ""
        for attempt in range(1, maximum_attempts + 1):
            completed = run(command, check=False)
            if completed.returncode == 0:
                try:
                    candidate_report, candidate_sha256 = load_clean_confirmation(
                        report_path,
                        jobs,
                        previous_generated_at,
                    )
                    wait_for_gate_confirmation(
                        report_path=report_path,
                        gate_state_path=gate_state_path,
                        report=candidate_report,
                        report_sha256=candidate_sha256,
                        jobs=jobs,
                        minimum_full_clean=confirmation,
                        wait_seconds=gate_wait_seconds,
                        sleep=sleep,
                    )
                except (OSError, ValueError, RuntimeError) as error:
                    print(f"Post-repair verification was not clean: {error}")
                else:
                    confirmed_report = candidate_report
                    confirmed_sha256 = candidate_sha256
                    break
            if attempt < maximum_attempts:
                print(
                    f"Post-repair verification attempt {attempt} failed; "
                    f"retrying in {retry_delay_seconds} seconds."
                )
                sleep(max(0, retry_delay_seconds))
        if confirmed_report is None:
            return int(completed.returncode or 1)
        previous_generated_at = confirmed_report["generated_at"]
        confirmation_reports.append(
            {
                "generated_at": previous_generated_at,
                "report_sha256": confirmed_sha256,
            }
        )
        print(
            f"Post-repair clean confirmation {confirmation} of "
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
                    "schema_version": 2,
                    "repair_report": repair_report,
                    "jobs": jobs,
                    "confirmations": required_confirmations,
                    "confirmation_reports": confirmation_reports,
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
        gate_state_path=args.gate_state,
        gate_wait_seconds=args.gate_wait_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
