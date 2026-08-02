#!/usr/bin/env python3
"""Record clean object-store inventories without controlling copy writers."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

CATALOG = Path("/etc/aurora-object-store/catalog.json")
STATE = Path("/var/lib/aurora-cloud/object-store-verification-gate/state.json")
REQUIRED = 2
POLICY_VERSION = 3


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    config = json.loads(CATALOG.read_text(encoding="utf-8"))
    report_path = Path(config["manifest_root"], "latest", "comparison.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    previous = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    generated_at = report["generated_at"]
    if (
        previous.get("last_generated_at") == generated_at
        and previous.get("policy_version") == POLICY_VERSION
    ):
        return 0

    failures: list[str] = []
    mode = report.get("verification_mode", "full")
    report_jobs = set(report.get("jobs", {}))
    verified_jobs = set(report.get("verified_jobs", report_jobs))
    if mode == "full":
        if verified_jobs != report_jobs:
            failures.append("full_report_does_not_verify_all_jobs")
    elif mode == "incremental":
        base_generated_at = report.get("base_generated_at")
        if not base_generated_at:
            failures.append("incremental_base_missing")
        elif base_generated_at != previous.get("last_generated_at"):
            failures.append("incremental_base_does_not_match_previous_report")
        if not report.get("base_report_sha256"):
            failures.append("incremental_base_hash_missing")
        if not verified_jobs or not verified_jobs <= report_jobs:
            failures.append("incremental_verified_jobs_invalid")
        previous_failed_jobs = {
            str(failure).split(":", 1)[0]
            for failure in previous.get("failures", [])
            if ":" in str(failure)
            and not str(failure).startswith(("report_", "incremental_"))
        }
        if not previous_failed_jobs <= verified_jobs:
            failures.append("incremental_does_not_refresh_all_failed_jobs")
        if int(report.get("incremental_depth", 0)) < 1:
            failures.append("incremental_depth_invalid")
    else:
        failures.append(f"verification_mode_invalid={mode}")
    for name, values in report.get("jobs", {}).items():
        for destination, comparison_name in (
            ("", "source_vs_s3"),
            ("gws_", "source_vs_gws"),
        ):
            # Raw GWS uses the independent per-stream verifier because its
            # canonical layout differs from cloud ingress.  Its all-age
            # comparison includes live files still in transit; that is useful
            # telemetry but must not invalidate seven-day retention proof.
            if name == "raw" and comparison_name == "source_vs_gws":
                continue
            comparison = values.get(comparison_name)
            if comparison is None:
                failures.append(f"{name}:{destination}evidence_missing")
                continue
            for field in (
                "missing_from_right",
                "size_mismatch",
                "checksum_mismatch",
            ):
                count = len(comparison.get(field, []))
                if count:
                    failures.append(
                        f"{name}:{destination}{field}={count}"
                    )

    gws_summary_path = Path(config["gws_manifest_root"], "latest", "summary.json")
    try:
        gws_summary = json.loads(gws_summary_path.read_text(encoding="utf-8"))
        for stream in config.get("streams", []):
            state = gws_summary.get("streams", {}).get(stream["name"], {})
            if state.get("error"):
                failures.append(f"raw:gws_verifier_error={stream['name']}")
                continue
            for field in (
                "retention_local_missing_count",
                "retention_local_mismatch_count",
                "retention_gws_missing_count",
                "retention_gws_mismatch_count",
            ):
                count = int(state.get(field, 0) or 0)
                if count:
                    failures.append(f"raw:gws_{field}:{stream['name']}={count}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"raw:gws_retention_evidence_unavailable={exc}")

    age_hours = (
        dt.datetime.now(dt.timezone.utc) - parse_time(generated_at)
    ).total_seconds() / 3600
    if age_hours > float(config.get("report_max_age_hours", 8)):
        failures.append(f"report_stale_hours={age_hours:.2f}")

    evidence_floor = report.get("evidence_floor_generated_at", generated_at)
    evidence_age_hours = (
        dt.datetime.now(dt.timezone.utc) - parse_time(evidence_floor)
    ).total_seconds() / 3600
    if evidence_age_hours > float(config.get("report_max_age_hours", 8)):
        failures.append(f"base_evidence_stale_hours={evidence_age_hours:.2f}")

    is_clean = not failures
    previous_streak = (
        int(previous.get("clean_streak", 0))
        if previous.get("policy_version") == POLICY_VERSION
        else 0
    )
    streak = previous_streak + 1 if is_clean else 0
    previous_full_reports = (
        int(previous.get("full_clean_reports_in_streak", 0))
        if previous.get("policy_version") == POLICY_VERSION
        else 0
    )
    full_clean_reports = (
        previous_full_reports + (1 if mode == "full" else 0)
        if is_clean
        else 0
    )
    state = {
        "schema_version": 3,
        "policy_version": POLICY_VERSION,
        "last_generated_at": generated_at,
        "clean": is_clean,
        "clean_streak": streak,
        "required_clean_reports": REQUIRED,
        "stable_parity": (
            is_clean and streak >= REQUIRED and full_clean_reports >= 1
        ),
        "failures": failures,
        "writers_policy": "independent",
        "verification_mode": mode,
        "verified_jobs": sorted(verified_jobs),
        "evidence_floor_generated_at": evidence_floor,
        "full_clean_reports_in_streak": full_clean_reports,
    }
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
