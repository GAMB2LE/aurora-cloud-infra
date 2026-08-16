#!/usr/bin/env python3
"""Record clean object-store inventories without controlling copy writers."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

CATALOG = Path("/etc/aurora-object-store/catalog.json")
STATE = Path("/var/lib/aurora-cloud/object-store-verification-gate/state.json")
REQUIRED = 2
POLICY_VERSION = 4


def domain_for_job(name: str) -> str:
    return "raw_retention" if name == "raw" else "products"


def previous_domain(previous: dict, name: str) -> dict:
    domains = previous.get("domains")
    if isinstance(domains, dict) and isinstance(domains.get(name), dict):
        return domains[name]
    # Migrate a clean v3 state conservatively.  It may seed a streak, but a
    # v4 report still has to establish domain-specific evidence before the
    # domain becomes ready.
    if previous.get("policy_version") == 3 and previous.get("clean"):
        return {
            "clean": True,
            "clean_streak": min(int(previous.get("clean_streak", 0)), 1),
            "stable_parity": False,
            "full_clean_reports_in_streak": 0,
        }
    return {}


def build_domain_state(
    *,
    previous: dict,
    failures: list[str],
    mode: str,
    generated_at: str,
    evidence_floor: str,
    verified: bool,
) -> dict:
    clean = not failures
    prior_streak = int(previous.get("clean_streak", 0))
    streak = prior_streak + 1 if clean else 0
    prior_full = int(previous.get("full_clean_reports_in_streak", 0))
    full_clean = prior_full + int(mode == "full") if clean else 0
    last_clean_at = generated_at if clean else previous.get("last_clean_at")
    return {
        "clean": clean,
        "clean_streak": streak,
        "required_clean_reports": REQUIRED,
        "stable_parity": clean and streak >= REQUIRED and full_clean >= 1,
        "failures": failures,
        "verification_mode": mode,
        "verified_in_report": verified,
        "evidence_floor_generated_at": evidence_floor,
        "full_clean_reports_in_streak": full_clean,
        "last_clean_at": last_clean_at,
    }


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

    common_failures: list[str] = []
    domain_failures: dict[str, list[str]] = {
        "raw_retention": [],
        "products": [],
    }
    mode = report.get("verification_mode", "full")
    report_jobs = set(report.get("jobs", {}))
    verified_jobs = set(report.get("verified_jobs", report_jobs))
    if mode == "full":
        if verified_jobs != report_jobs:
            common_failures.append("full_report_does_not_verify_all_jobs")
    elif mode == "incremental":
        base_generated_at = report.get("base_generated_at")
        if not base_generated_at:
            common_failures.append("incremental_base_missing")
        elif base_generated_at != previous.get("last_generated_at"):
            common_failures.append("incremental_base_does_not_match_previous_report")
        if not report.get("base_report_sha256"):
            common_failures.append("incremental_base_hash_missing")
        if not verified_jobs or not verified_jobs <= report_jobs:
            common_failures.append("incremental_verified_jobs_invalid")
        previous_failed_jobs = {
            str(failure).split(":", 1)[0]
            for failure in previous.get("failures", [])
            if ":" in str(failure)
            and not str(failure).startswith(("report_", "incremental_"))
        }
        if not previous_failed_jobs <= verified_jobs:
            common_failures.append("incremental_does_not_refresh_all_failed_jobs")
        if int(report.get("incremental_depth", 0)) < 1:
            common_failures.append("incremental_depth_invalid")
    else:
        common_failures.append(f"verification_mode_invalid={mode}")
    for name, values in report.get("jobs", {}).items():
        domain = domain_for_job(name)
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
                domain_failures[domain].append(
                    f"{name}:{destination}evidence_missing"
                )
                continue
            for field in (
                "missing_from_right",
                "size_mismatch",
                "checksum_mismatch",
            ):
                count = len(comparison.get(field, []))
                if count:
                    domain_failures[domain].append(
                        f"{name}:{destination}{field}={count}"
                    )

    gws_summary_path = Path(config["gws_manifest_root"], "latest", "summary.json")
    try:
        gws_summary = json.loads(gws_summary_path.read_text(encoding="utf-8"))
        for stream in config.get("streams", []):
            state = gws_summary.get("streams", {}).get(stream["name"], {})
            if state.get("error"):
                domain_failures["raw_retention"].append(
                    f"raw:gws_verifier_error={stream['name']}"
                )
                continue
            for field in (
                "retention_local_missing_count",
                "retention_local_mismatch_count",
                "retention_gws_missing_count",
                "retention_gws_mismatch_count",
            ):
                count = int(state.get(field, 0) or 0)
                if count:
                    domain_failures["raw_retention"].append(
                        f"raw:gws_{field}:{stream['name']}={count}"
                    )
    except Exception as exc:  # noqa: BLE001
        domain_failures["raw_retention"].append(
            f"raw:gws_retention_evidence_unavailable={exc}"
        )

    age_hours = (
        dt.datetime.now(dt.timezone.utc) - parse_time(generated_at)
    ).total_seconds() / 3600
    if age_hours > float(config.get("report_max_age_hours", 8)):
        common_failures.append(f"report_stale_hours={age_hours:.2f}")

    evidence_floor = report.get("evidence_floor_generated_at", generated_at)
    evidence_age_hours = (
        dt.datetime.now(dt.timezone.utc) - parse_time(evidence_floor)
    ).total_seconds() / 3600
    evidence_max_age = float(
        config.get(
            "incremental_base_max_age_hours",
            config.get("report_max_age_hours", 8),
        )
        if mode == "incremental"
        else config.get("report_max_age_hours", 8)
    )
    if evidence_age_hours > evidence_max_age:
        common_failures.append(
            f"base_evidence_stale_hours={evidence_age_hours:.2f}"
        )

    domains = {}
    for name in ("raw_retention", "products"):
        failures = [*common_failures, *domain_failures[name]]
        domains[name] = build_domain_state(
            previous=previous_domain(previous, name),
            failures=failures,
            mode=mode,
            generated_at=generated_at,
            evidence_floor=evidence_floor,
            verified=any(domain_for_job(job) == name for job in verified_jobs),
        )

    failures = [
        *common_failures,
        *domain_failures["raw_retention"],
        *domain_failures["products"],
    ]
    is_clean = all(value["clean"] for value in domains.values())
    streak = min(value["clean_streak"] for value in domains.values())
    full_clean_reports = min(
        value["full_clean_reports_in_streak"] for value in domains.values()
    )
    state = {
        "schema_version": 4,
        "policy_version": POLICY_VERSION,
        "last_generated_at": generated_at,
        "clean": is_clean,
        "clean_streak": streak,
        "required_clean_reports": REQUIRED,
        "stable_parity": all(
            value["stable_parity"] for value in domains.values()
        ),
        "raw_retention_ready": domains["raw_retention"]["stable_parity"],
        "products_stable_parity": domains["products"]["stable_parity"],
        "failures": failures,
        "writers_policy": "independent",
        "verification_mode": mode,
        "verified_jobs": sorted(verified_jobs),
        "evidence_floor_generated_at": evidence_floor,
        "full_clean_reports_in_streak": full_clean_reports,
        "domains": domains,
    }
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
