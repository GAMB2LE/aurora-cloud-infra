#!/usr/bin/env python3
"""Record clean object-store inventories without controlling copy writers."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

CATALOG = Path("/etc/aurora-object-store/catalog.json")
STATE = Path("/var/lib/aurora-cloud/object-store-verification-gate/state.json")
REQUIRED = 2


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    config = json.loads(CATALOG.read_text(encoding="utf-8"))
    report_path = Path(config["manifest_root"], "latest", "comparison.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    previous = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    generated_at = report["generated_at"]
    if previous.get("last_generated_at") == generated_at:
        return 0

    failures: list[str] = []
    for name, values in report.get("jobs", {}).items():
        comparison = values.get("source_vs_s3") or {}
        for field in ("missing_from_right", "size_mismatch", "checksum_mismatch"):
            count = len(comparison.get(field, []))
            if count:
                failures.append(f"{name}:{field}={count}")

    age_hours = (
        dt.datetime.now(dt.timezone.utc) - parse_time(generated_at)
    ).total_seconds() / 3600
    if age_hours > float(config.get("report_max_age_hours", 8)):
        failures.append(f"report_stale_hours={age_hours:.2f}")

    is_clean = not failures
    streak = int(previous.get("clean_streak", 0)) + 1 if is_clean else 0
    state = {
        "schema_version": 2,
        "last_generated_at": generated_at,
        "clean": is_clean,
        "clean_streak": streak,
        "required_clean_reports": REQUIRED,
        "stable_parity": is_clean and streak >= REQUIRED,
        "failures": failures,
        "writers_policy": "independent",
    }
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
