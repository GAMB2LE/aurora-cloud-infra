#!/usr/bin/env python3
"""Record clean object-store inventories without controlling copy writers."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

CATALOG = Path("/etc/aurora-object-store/catalog.json")
STATE = Path("/var/lib/aurora-cloud/object-store-verification-gate/state.json")
REQUIRED = 2
POLICY_VERSION = 5


def domain_for_job(name: str) -> str:
    return "raw_retention" if name == "raw" else "products"


def previous_domain(previous: dict, name: str) -> dict:
    domains = previous.get("domains")
    if isinstance(domains, dict) and isinstance(domains.get(name), dict):
        result = dict(domains[name])
        # Policy v4 reset a domain's streak when a shared merge-base timestamp
        # aged out, even when the domain's last check was clean and contained
        # no measured gap. Preserve one such clean observation during the v5
        # migration; a current complete family audit is still required before
        # stable parity can be restored.
        stale_only = result.get("failures") and all(
            str(failure).startswith(
                ("report_stale_hours=", "base_evidence_stale_hours=")
            )
            for failure in result.get("failures", [])
        )
        if (
            previous.get("policy_version") == 4
            and result.get("last_clean_at")
            and stale_only
        ):
            result["clean_streak"] = max(
                1, int(result.get("clean_streak", 0))
            )
            result["full_clean_reports_in_streak"] = max(
                1, int(result.get("full_clean_reports_in_streak", 0))
            )
        return result
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
    evidence_id: str,
    verified: bool,
    complete_verification: bool,
) -> dict:
    clean = not failures
    prior_streak = int(previous.get("clean_streak", 0))
    prior_full = int(previous.get("full_clean_reports_in_streak", 0))
    evidence_changed = evidence_id != previous.get("evidence_id")
    if not clean:
        streak = 0
        full_clean = 0
    elif verified and evidence_changed:
        streak = prior_streak + 1
        full_clean = prior_full + int(complete_verification)
    else:
        # Reused evidence is neither a new success nor a new failure.  In
        # particular, a products checkpoint must not inflate the raw streak.
        streak = prior_streak
        full_clean = prior_full
    last_clean_at = (
        generated_at
        if clean and verified and evidence_changed
        else previous.get("last_clean_at")
    )
    return {
        "clean": clean,
        "clean_streak": streak,
        "required_clean_reports": REQUIRED,
        "stable_parity": clean and streak >= REQUIRED and full_clean >= 1,
        "failures": failures,
        "verification_mode": mode,
        "verified_in_report": verified,
        "complete_verification": complete_verification,
        "evidence_floor_generated_at": evidence_floor,
        "evidence_id": evidence_id,
        "full_clean_reports_in_streak": full_clean,
        "last_clean_at": last_clean_at,
    }


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def history_id(value: str) -> str:
    return value.replace(":", "").replace("-", "")


def incremental_base_is_trusted(
    *,
    manifest_root: Path,
    base_generated_at: str,
    expected_sha256: str,
    previous_generated_at: str | None,
    previous_report_sha256: str | None,
) -> bool:
    """Accept the prior gate state or its retained immutable snapshot.

    Path-unit events may coalesce while one full service publishes several
    family checkpoints.  The gate can therefore legitimately observe
    checkpoint N without having evaluated checkpoint N-1.  Its exact base is
    retained in history and hash-bound by the report.
    """
    if base_generated_at == previous_generated_at and previous_report_sha256:
        return expected_sha256 == previous_report_sha256
    snapshot = (
        manifest_root
        / "history"
        / history_id(base_generated_at)
        / "comparison.json"
    )
    try:
        digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    except OSError:
        return False
    return digest == expected_sha256


def main() -> int:
    config = json.loads(CATALOG.read_text(encoding="utf-8"))
    manifest_root = Path(config["manifest_root"])
    report_path = manifest_root / "latest" / "comparison.json"
    report_bytes = report_path.read_bytes()
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    report = json.loads(report_bytes)
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
        base_report_sha256 = report.get("base_report_sha256")
        if not base_report_sha256:
            common_failures.append("incremental_base_hash_missing")
        elif base_generated_at and not incremental_base_is_trusted(
            manifest_root=manifest_root,
            base_generated_at=base_generated_at,
            expected_sha256=base_report_sha256,
            previous_generated_at=previous.get("last_generated_at"),
            previous_report_sha256=previous.get("report_sha256"),
        ):
            common_failures.append(
                "incremental_base_does_not_match_previous_report"
            )
        if not verified_jobs or not verified_jobs <= report_jobs:
            common_failures.append("incremental_verified_jobs_invalid")
        if int(report.get("incremental_depth", 0)) < 1:
            common_failures.append("incremental_depth_invalid")
    else:
        common_failures.append(f"verification_mode_invalid={mode}")
    legacy_evidence_floor = report.get(
        "evidence_floor_generated_at", generated_at
    )
    domain_evidence: dict[str, dict[str, str]] = {
        "raw_retention": {},
        "products": {},
    }
    domain_jobs: dict[str, set[str]] = {
        "raw_retention": set(),
        "products": set(),
    }
    for name, values in report.get("jobs", {}).items():
        domain = domain_for_job(name)
        domain_jobs[domain].add(name)
        evidence_at = values.get("verified_at", legacy_evidence_floor)
        try:
            parse_time(evidence_at)
        except (TypeError, ValueError):
            domain_failures[domain].append(
                f"{name}:verification_timestamp_invalid"
            )
        else:
            domain_evidence[domain][name] = evidence_at
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
    gws_generated_at: str | None = None
    try:
        gws_summary = json.loads(gws_summary_path.read_text(encoding="utf-8"))
        gws_generated_at = gws_summary.get("generated_at")
        if not gws_generated_at:
            domain_failures["raw_retention"].append(
                "raw:gws_verification_timestamp_missing"
            )
        else:
            parse_time(gws_generated_at)
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

    domain_evidence_floor: dict[str, str] = {}
    domain_evidence_id: dict[str, str] = {}
    evidence_max_age = float(config.get("report_max_age_hours", 8))
    now = dt.datetime.now(dt.timezone.utc)
    for name, values_by_job in domain_evidence.items():
        if not domain_jobs[name]:
            domain_evidence_floor[name] = legacy_evidence_floor
            domain_evidence_id[name] = json.dumps(
                {"legacy": legacy_evidence_floor}, sort_keys=True
            )
            continue
        evidence_times = list(values_by_job.values())
        if name == "raw_retention" and gws_generated_at:
            evidence_times.append(gws_generated_at)
        if len(evidence_times) < len(domain_jobs[name]) + int(
            name == "raw_retention"
        ):
            domain_failures[name].append(f"{name}:evidence_timestamp_missing")
            domain_evidence_floor[name] = legacy_evidence_floor
            domain_evidence_id[name] = json.dumps(
                values_by_job, sort_keys=True
            )
            continue
        evidence_floor = min(evidence_times, key=parse_time)
        domain_evidence_floor[name] = evidence_floor
        # The event identity changes only when a family itself is rechecked.
        # A newer independent GWS summary is required for raw readiness and
        # contributes to its evidence age, but cannot manufacture a second
        # object-store clean observation for the same raw-family audit.
        domain_evidence_id[name] = json.dumps(values_by_job, sort_keys=True)
        evidence_age_hours = (
            now - parse_time(evidence_floor)
        ).total_seconds() / 3600
        if evidence_age_hours > evidence_max_age:
            domain_failures[name].append(
                f"{name}_evidence_stale_hours={evidence_age_hours:.2f}"
            )

    domains: dict[str, dict] = {}
    for name in ("raw_retention", "products"):
        failures = [*common_failures, *domain_failures[name]]
        verified_domain_jobs = domain_jobs[name] & verified_jobs
        complete_verification = (
            bool(domain_jobs[name])
            and domain_jobs[name] <= verified_jobs
            and all(
                report["jobs"][job].get(
                    "verification_scope", "full_family"
                )
                == "full_family"
                for job in domain_jobs[name]
            )
        )
        domains[name] = build_domain_state(
            previous=previous_domain(previous, name),
            failures=failures,
            mode=mode,
            generated_at=generated_at,
            evidence_floor=domain_evidence_floor.get(
                name, legacy_evidence_floor
            ),
            evidence_id=domain_evidence_id.get(
                name,
                json.dumps({"legacy": legacy_evidence_floor}, sort_keys=True),
            ),
            verified=bool(verified_domain_jobs),
            complete_verification=complete_verification,
        )

    failures = [
        *common_failures,
        *domain_failures["raw_retention"],
        *domain_failures["products"],
    ]
    active_domains = [
        name for name, jobs in domain_jobs.items() if jobs
    ]
    is_clean = all(domains[name]["clean"] for name in active_domains)
    streak = min(domains[name]["clean_streak"] for name in active_domains)
    full_clean_reports = min(
        domains[name]["full_clean_reports_in_streak"]
        for name in active_domains
    )
    state = {
        "schema_version": 5,
        "policy_version": POLICY_VERSION,
        "last_generated_at": generated_at,
        "report_sha256": report_sha256,
        "clean": is_clean,
        "clean_streak": streak,
        "required_clean_reports": REQUIRED,
        "stable_parity": all(
            domains[name]["stable_parity"] for name in active_domains
        ),
        "raw_retention_ready": domains["raw_retention"]["stable_parity"],
        "products_stable_parity": (
            domains["products"]["stable_parity"]
            if domain_jobs["products"]
            else True
        ),
        "failures": failures,
        "writers_policy": "independent",
        "verification_mode": mode,
        "verified_jobs": sorted(verified_jobs),
        "evidence_floor_generated_at": min(
            domain_evidence_floor.values(), key=parse_time
        ),
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
