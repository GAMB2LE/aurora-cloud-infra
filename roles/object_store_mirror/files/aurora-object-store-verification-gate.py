#!/usr/bin/env python3
"""Evaluate independent, complete archive-family observations (policy 7)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

CATALOG = Path("/etc/aurora-object-store/catalog.json")
STATE = Path("/var/lib/aurora-cloud/object-store-verification-gate/state.json")
REQUIRED = 2
POLICY_VERSION = 7
UTC = dt.timezone.utc


def parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires a timezone")
    return parsed


def domain_for_job(name: str) -> str:
    return "raw_retention" if name == "raw" else "products"


def history_id(value: str) -> str:
    return value.replace(":", "").replace("-", "")


def incremental_base_is_trusted(*, manifest_root: Path, base_generated_at: str,
                                expected_sha256: str, previous_generated_at: str | None,
                                previous_report_sha256: str | None) -> bool:
    if base_generated_at == previous_generated_at and previous_report_sha256:
        return expected_sha256 == previous_report_sha256
    snapshot = manifest_root / "history" / history_id(base_generated_at) / "comparison.json"
    try:
        return hashlib.sha256(snapshot.read_bytes()).hexdigest() == expected_sha256
    except OSError:
        return False


def load_evaluation_inputs(config: dict, report: dict | None = None,
                           previous: dict | None = None) -> dict:
    """Load external evidence; evaluate() itself performs no I/O."""
    result = dict(config)
    try:
        result["_gws_summary"] = json.loads(
            (Path(config["gws_manifest_root"]) / "latest/summary.json").read_text()
        )
    except (OSError, ValueError, KeyError) as error:
        result["_gws_error"] = str(error)
    if report and report.get("verification_mode") == "incremental":
        previous = previous or {}
        result["_incremental_base_trusted"] = incremental_base_is_trusted(
            manifest_root=Path(config["manifest_root"]),
            base_generated_at=report.get("base_generated_at", ""),
            expected_sha256=report.get("base_report_sha256", ""),
            previous_generated_at=previous.get("last_generated_at"),
            previous_report_sha256=previous.get("report_sha256"),
        )
    return result


def evaluate(config: dict, report: dict, previous: dict, *, report_sha256: str,
             now: dt.datetime | None = None) -> dict:
    """Pure evaluator; independent GWS summary arrives in config['_gws_summary'].

    Domain counters are minima of constituent family counters. Replaying a
    report rechecks age without adding confirmations. Legacy domain streaks
    cannot manufacture family history.
    """
    now = now or dt.datetime.now(UTC)
    generated = report.get("generated_at", "")
    common = []
    jobs = report.get("jobs", {})
    if not isinstance(jobs, dict):
        jobs = {}
        common.append("report_jobs_invalid")
    catalog = config.get("jobs", [])
    if not isinstance(catalog, list):
        catalog = []
    names = [job.get("name") for job in catalog if isinstance(job, dict)]
    valid_names = [n for n in names if isinstance(n, str) and n]
    if (not names or len(names) != len(catalog) or len(valid_names) != len(names)
            or len(set(valid_names)) != len(valid_names)):
        common.append("configured_catalogue_invalid")
        names = valid_names
    configured = set(names)
    if set(jobs) - configured:
        common.append("report_contains_unconfigured_jobs")
    mode = report.get("verification_mode", "full")
    verified_values = report.get("verified_jobs", list(jobs))
    if not isinstance(verified_values, list) or not all(isinstance(x, str) for x in verified_values):
        verified_values = []
    verified = set(verified_values)
    if not verified or not verified <= set(jobs):
        common.append("verified_jobs_invalid")
    if mode == "full":
        if verified != set(jobs):
            common.append("full_report_does_not_verify_all_jobs")
    elif mode == "incremental":
        if not report.get("base_generated_at"):
            common.append("incremental_base_missing")
        if not report.get("base_report_sha256"):
            common.append("incremental_base_hash_missing")
        elif not (config.get("_incremental_base_trusted") or
                  (report.get("base_generated_at") == previous.get("last_generated_at") and
                   report.get("base_report_sha256") == previous.get("report_sha256")) or
                  report_sha256 == previous.get("report_sha256")):
            common.append("incremental_base_does_not_match_previous_report")
        if not isinstance(report.get("incremental_depth"), int) or report["incremental_depth"] < 1:
            common.append("incremental_depth_invalid")
    else:
        common.append(f"verification_mode_invalid={mode}")
    try:
        parse_time(generated)
    except (AttributeError, TypeError, ValueError):
        common.append("report_timestamp_invalid")

    families = {}
    horizons = config.get("domain_evidence_max_age_hours", {})
    if not isinstance(horizons, dict):
        horizons = {}
    for name in sorted(configured):
        domain = domain_for_job(name)
        failures = list(common)
        value = jobs.get(name)
        if not isinstance(value, dict):
            value = {}
            failures.append(f"{name}:configured_family_missing")
        if value.get("verification_scope") != "full_family":
            failures.append(f"{name}:verification_scope_not_full_family")
        # Legacy verified_at was collection completion, not observation start.
        # It cannot safely seed a certificate whose age starts before that.
        started = value.get("evidence_started_at")
        start_trusted = isinstance(started, str) and bool(started)
        if not start_trusted:
            failures.append(f"{name}:observation_start_untrusted")
        completed = value.get("verification_completed_at", value.get("verified_at"))
        identity = value.get("verification_id") or f"legacy:{name}:{value.get('verified_at', '')}"
        if not isinstance(identity, str):
            failures.append(f"{name}:verification_id_invalid")
            identity = "invalid"
        try:
            max_age = float(horizons.get(domain, 8 if name == "raw" else 36))
            if not 0 < max_age < float("inf"):
                raise ValueError("nonpositive horizon")
        except (ValueError, TypeError):
            max_age = 8 if name == "raw" else 36
            failures.append(f"{name}:evidence_max_age_invalid")
        age = None
        try:
            start_time, completion_time = parse_time(started), parse_time(completed)
            age = (now - start_time).total_seconds() / 3600
            if completion_time < start_time or completion_time > now + dt.timedelta(minutes=5):
                failures.append(f"{name}:verification_observation_order_invalid")
            if completion_time - start_time > dt.timedelta(hours=4 if name == "raw" else 12):
                failures.append(f"{name}:verification_observation_window_exceeded")
            if age >= max_age:
                failures.append(f"{domain}_evidence_stale_hours={age:.2f}")
        except (AttributeError, TypeError, ValueError):
            failures.append(f"{name}:verification_timestamp_invalid")
        for label, field in (("", "source_vs_s3"), ("gws_", "source_vs_gws")):
            if name == "raw" and field == "source_vs_gws":
                continue
            comparison = value.get(field)
            if not isinstance(comparison, dict):
                failures.append(f"{name}:{label}evidence_missing")
                continue
            for counter in ("missing_from_right", "size_mismatch", "checksum_mismatch"):
                entries = comparison.get(counter)
                if not isinstance(entries, list):
                    failures.append(f"{name}:{label}{counter}_invalid")
                elif entries:
                    failures.append(f"{name}:{label}{counter}={len(entries)}")
        prior = previous.get("families", {}).get(name, {}) if previous.get("policy_version") == POLICY_VERSION else {}
        prior_observations = prior.get("observations", [])
        if not isinstance(prior_observations, list):
            prior_observations = []
        prior_observations = [entry for entry in prior_observations if isinstance(entry, dict)]
        ids = {entry.get("verification_id") for entry in prior_observations}
        ids.add(prior.get("verification_id"))
        watermark = prior.get("last_observation_completed_at", prior.get("verification_completed_at"))
        new = (not prior or name in verified) and identity not in ids
        if watermark and new:
            try:
                new = parse_time(completed) > parse_time(watermark)
            except (TypeError, ValueError, AttributeError):
                new = False
        observations = []
        seen = set()
        for entry in prior_observations:
            try:
                observed = parse_time(entry["evidence_started_at"])
                finished = parse_time(entry["verification_completed_at"])
                eligible = (entry.get("start_trusted") is True
                            and isinstance(entry.get("verification_id"), str)
                            and entry["verification_id"] not in seen
                            and observed <= finished <= now + dt.timedelta(minutes=5)
                            and finished - observed <= dt.timedelta(hours=4 if name == "raw" else 12)
                            and dt.timedelta(0) <= now - observed < dt.timedelta(hours=max_age))
            except (TypeError, ValueError, KeyError, AttributeError):
                eligible = False
            if eligible:
                observations.append(entry)
                seen.add(entry["verification_id"])
        if failures:
            observations = []
        if not failures and new:
            observations = [*observations, {"verification_id": identity,
                "evidence_started_at": started, "verification_completed_at": completed,
                "start_trusted": True}][-REQUIRED:]
        streak = len(observations)
        confirmation_floor = min((entry["evidence_started_at"] for entry in observations),
                                 key=parse_time, default=None)
        confirmation_expiry = ((parse_time(confirmation_floor) + dt.timedelta(hours=max_age)).isoformat()
                               if confirmation_floor else None)
        try:
            watermark = max((value for value in (watermark, completed) if value), key=parse_time)
        except (TypeError, ValueError, AttributeError):
            pass
        families[name] = {
            "clean": not failures, "clean_streak": streak,
            "stable_parity": not failures and streak >= REQUIRED and len(observations) >= REQUIRED,
            "required_clean_reports": REQUIRED, "failures": failures,
            "verification_id": identity, "evidence_started_at": started,
            "evidence_start_trusted": start_trusted,
            "verification_completed_at": completed, "evidence_max_age_hours": max_age,
            "evidence_age_hours": age, "observations": observations,
            "confirmation_evidence_started_at": confirmation_floor,
            "confirmation_expires_at": confirmation_expiry,
            "last_observation_completed_at": watermark,
            "verified_in_report": name in verified,
            "last_clean_at": completed if not failures and new else prior.get("last_clean_at"),
        }

    gws_failures = []
    gws_at = None
    if "raw" in configured:
        summary = config.get("_gws_summary")
        if not isinstance(summary, dict):
            gws_failures.append(f"raw:gws_retention_evidence_unavailable={config.get('_gws_error', 'not supplied')}")
        else:
            try:
                gws_at = summary.get("generated_at")
                age = (now - parse_time(gws_at)).total_seconds() / 3600
                if age >= families["raw"]["evidence_max_age_hours"]:
                    gws_failures.append(f"raw_retention_evidence_stale_hours={age:.2f}")
                elif age < -5 / 60:
                    gws_failures.append("raw:gws_verification_timestamp_future")
            except (TypeError, ValueError, AttributeError):
                gws_at = None
                gws_failures.append("raw:gws_verification_timestamp_invalid")
            for stream in config.get("streams", []):
                name = stream["name"]
                state = summary.get("streams", {}).get(name, {})
                if state.get("error"):
                    gws_failures.append(f"raw:gws_verifier_error={name}")
                for field in ("retention_local_missing_count", "retention_local_mismatch_count",
                              "retention_gws_missing_count", "retention_gws_mismatch_count"):
                    count = state.get(field)
                    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                        gws_failures.append(f"raw:gws_{field}:{name}=invalid")
                    elif count:
                        gws_failures.append(f"raw:gws_{field}:{name}={count}")
    domains = {}
    for domain in ("raw_retention", "products"):
        members = {n: f for n, f in families.items() if domain_for_job(n) == domain}
        failures = list(dict.fromkeys([*common, *(x for f in members.values() for x in f["failures"]),
                                      *(gws_failures if domain == "raw_retention" else [])]))
        floors = [f["confirmation_evidence_started_at"] if f["stable_parity"] else f["evidence_started_at"]
                  for f in members.values() if f["evidence_started_at"]]
        if domain == "raw_retention" and gws_at:
            floors.append(gws_at)
        try:
            floor = min(floors, key=parse_time) if floors else generated
        except (ValueError, AttributeError, TypeError):
            floor = generated
        streak = min((f["clean_streak"] for f in members.values()), default=0)
        complete = bool(members) and all(jobs.get(n, {}).get("verification_scope") == "full_family" for n in members)
        domains[domain] = {
            "clean": bool(members) and not failures, "clean_streak": streak,
            "stable_parity": complete and not failures and all(f["stable_parity"] for f in members.values()),
            "failures": failures, "required_clean_reports": REQUIRED,
            "verification_mode": mode, "verified_in_report": bool(set(members) & verified),
            "complete_verification": complete, "evidence_floor_generated_at": floor,
            "evidence_max_age_hours": min((f["evidence_max_age_hours"] for f in members.values()), default=8 if domain == "raw_retention" else 36),
            "evidence_id": json.dumps({n: f["verification_id"] for n, f in members.items()}, sort_keys=True),
            "full_clean_reports_in_streak": streak,
            "last_clean_at": max((f["last_clean_at"] for f in members.values() if f["last_clean_at"]), default=None),
            "families": sorted(members),
        }
    active = [domains[d] for d in domains if any(domain_for_job(n) == d for n in configured)]
    failures = list(dict.fromkeys([*common, *(x for d in active for x in d["failures"])]))
    floors = [d["evidence_floor_generated_at"] for d in active]
    try:
        floor = min(floors, key=parse_time) if floors else generated
    except (ValueError, TypeError, AttributeError):
        floor = generated
    return {
        "schema_version": 6, "policy_version": POLICY_VERSION, "last_generated_at": generated,
        "evaluated_at": now.isoformat(), "report_sha256": report_sha256,
        "clean": bool(active) and not failures,
        "clean_streak": min((d["clean_streak"] for d in active), default=0),
        "required_clean_reports": REQUIRED,
        "stable_parity": bool(active) and all(d["stable_parity"] for d in active),
        "raw_retention_ready": domains["raw_retention"]["stable_parity"],
        "products_stable_parity": domains["products"]["stable_parity"],
        "failures": failures, "writers_policy": "independent", "verification_mode": mode,
        "verified_jobs": sorted(verified), "evidence_floor_generated_at": floor,
        "full_clean_reports_in_streak": min((d["clean_streak"] for d in active), default=0),
        "domains": domains, "families": families,
    }


def main() -> int:
    config = json.loads(CATALOG.read_text())
    config["gate_state_path"] = str(STATE)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, "/usr/local/lib/aurora-object-store")
    from aurora_object_store_evidence import refresh_gate
    try:
        refresh_gate(config)
    except BlockingIOError:
        # Another short publication owns the commit lock. The coordinator
        # will reevaluate on its next tick; contention is not an audit error.
        print(json.dumps({"state": "deferred", "reason": "publication_lock_busy"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
