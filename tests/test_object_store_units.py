import datetime as dt
from pathlib import Path
import unittest


TEMPLATES = (
    Path(__file__).parents[1] / "roles" / "object_store_mirror" / "templates"
)
FILES = Path(__file__).parents[1] / "roles" / "object_store_mirror" / "files"
GROUP_VARS = (
    Path(__file__).parents[1] / "inventory" / "group_vars" / "aurora_cloud.yml"
)
RETENTION_TEMPLATE = (
    Path(__file__).parents[1]
    / "roles"
    / "ass_retention"
    / "templates"
    / "aurora-ass-retention.py.j2"
)
HEALTH_TEMPLATE = (
    Path(__file__).parents[1]
    / "roles"
    / "operations_monitor"
    / "templates"
    / "aurora-archive-health.py.j2"
)


def load_retention_evidence_helpers():
    source = RETENTION_TEMPLATE.read_text(encoding="utf-8")
    helper_source = "def timestamp" + source.split("def timestamp", 1)[1].split(
        "\n\ndef fail", 1
    )[0]
    namespace = {"dt": dt}
    exec(helper_source, namespace)
    return (
        namespace["raw_evidence_time"],
        namespace["raw_evidence_is_fresh"],
        namespace["bounded_permit_expiry"],
    )


(
    raw_evidence_time,
    raw_evidence_is_fresh,
    bounded_permit_expiry,
) = load_retention_evidence_helpers()


class ObjectStoreUnitTests(unittest.TestCase):
    def test_inventory_units_use_systemd_credentials_for_gws_ssh(self) -> None:
        for name in (
            "aurora-object-store-inventory.service.j2",
            "aurora-object-store-inventory-incremental@.service.j2",
            "aurora-object-store-recheck-after-repair.service.j2",
        ):
            with self.subTest(name=name):
                source = (TEMPLATES / name).read_text(encoding="utf-8")
                self.assertIn("LoadCredential=gws-key:", source)
                self.assertIn("LoadCredential=gws-known-hosts:", source)
                self.assertNotIn("BindReadOnlyPaths", source)

    def test_full_audit_evaluates_last_checkpoint_even_on_failure(self) -> None:
        source = (
            TEMPLATES / "aurora-object-store-inventory.service.j2"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "ExecStopPost=+/usr/local/sbin/aurora-object-store-verification-gate",
            source,
        )
        self.assertIn(
            "ExecStopPost=+/usr/local/sbin/aurora-object-store-trigger-retention",
            source,
        )

    def test_incremental_inventory_allows_full_family_runtime(self) -> None:
        source = (
            TEMPLATES
            / "aurora-object-store-inventory-incremental@.service.j2"
        ).read_text(encoding="utf-8")

        self.assertIn("TimeoutStartSec=12h", source)
        self.assertNotIn("TimeoutStartSec=4h", source)

    def test_production_listing_timeout_allows_large_cl61_shards(self) -> None:
        source = GROUP_VARS.read_text(encoding="utf-8")

        self.assertIn(
            "object_store_inventory_list_timeout_seconds: 7200",
            source,
        )
        self.assertNotIn(
            "object_store_inventory_list_timeout_seconds: 3600",
            source,
        )

    def test_inventory_uses_domain_specific_evidence_horizons(self) -> None:
        group_vars = GROUP_VARS.read_text(encoding="utf-8")
        catalog = (TEMPLATES / "object-store-catalog.json.j2").read_text(
            encoding="utf-8"
        )
        health = HEALTH_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn(
            "object_store_inventory_raw_evidence_max_age_hours: 8",
            group_vars,
        )
        self.assertIn(
            "object_store_inventory_products_evidence_max_age_hours: 36",
            group_vars,
        )
        self.assertIn("ass_retention_manifest_max_age_hours: 8", group_vars)
        self.assertIn('"domain_evidence_max_age_hours": {', catalog)
        self.assertIn('"raw_retention": {{ object_store_inventory_raw_', catalog)
        self.assertIn('"products": {{ object_store_inventory_products_', catalog)
        self.assertIn(
            "RAW_EVIDENCE_MAX_AGE_HOURS = {{ "
            "object_store_inventory_raw_evidence_max_age_hours",
            health,
        )
        self.assertIn(
            "OBJECT_REPORT_MAX_AGE_HOURS = {{ "
            "object_store_inventory_products_evidence_max_age_hours",
            health,
        )
        self.assertIn("gws_age > RAW_EVIDENCE_MAX_AGE_HOURS", health)
        self.assertIn("object_age > OBJECT_REPORT_MAX_AGE_HOURS", health)

    def test_recheck_unit_can_persist_confirmation_state(self) -> None:
        source = (
            TEMPLATES / "aurora-object-store-recheck-after-repair.service.j2"
        ).read_text(encoding="utf-8")
        read_write_paths = next(
            line for line in source.splitlines() if line.startswith("ReadWritePaths=")
        )

        self.assertIn("{{ object_store_repair_state_root }}", read_write_paths)

    def test_recheck_unit_allows_two_long_family_confirmations(self) -> None:
        source = (
            TEMPLATES / "aurora-object-store-recheck-after-repair.service.j2"
        ).read_text(encoding="utf-8")
        group_vars = GROUP_VARS.read_text(encoding="utf-8")

        self.assertIn(
            "TimeoutStartSec={{ object_store_post_repair_recheck_timeout_start_sec }}",
            source,
        )
        self.assertIn(
            "object_store_post_repair_recheck_timeout_start_sec: 36h",
            group_vars,
        )
        self.assertIn(
            "--gate-wait-seconds {{ object_store_post_repair_gate_wait_seconds }}",
            source,
        )
        self.assertIn(
            "object_store_post_repair_gate_wait_seconds: 120",
            group_vars,
        )
        self.assertNotIn("TimeoutStartSec=4h", source)

    def test_repair_serializes_latest_read_and_result_publication(self) -> None:
        source = (FILES / "aurora-object-store-repair-from-report.py").read_text(
            encoding="utf-8"
        )

        lock = source.index("with inventory_lock(catalog):")
        read = source.index('args.report.read_text(encoding="utf-8")')
        publish = source.index("publish_result(args.result, payload)")
        self.assertIn('Path(catalog["manifest_root"]) / ".inventory.lock"', source)
        self.assertIn('lock_path.open("r", encoding="utf-8")', source)
        self.assertIn("fcntl.LOCK_EX", source)
        self.assertLess(lock, read)
        self.assertLess(read, publish)

    def test_retention_uses_the_independent_raw_gate(self) -> None:
        source = RETENTION_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn('"raw_retention_ready"', source)
        self.assertIn('object_gate.get("stable_parity", False)', source)
        self.assertIn("raw_evidence_is_fresh(object_gate, now, MAX_AGE)", source)

    def test_retention_rechecks_raw_domain_evidence_age_at_execution(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        gate = {
            "raw_retention_ready": True,
            "domains": {
                "raw_retention": {
                    "evidence_floor_generated_at": (
                        now - dt.timedelta(hours=9)
                    ).isoformat()
                }
            },
        }

        self.assertEqual(
            raw_evidence_time(gate),
            now - dt.timedelta(hours=9),
        )
        self.assertFalse(raw_evidence_is_fresh(gate, now, dt.timedelta(hours=8)))

        gate["domains"]["raw_retention"]["evidence_floor_generated_at"] = (
            now - dt.timedelta(hours=7)
        ).isoformat()
        self.assertTrue(raw_evidence_is_fresh(gate, now, dt.timedelta(hours=8)))

    def test_retention_permit_cannot_outlive_raw_evidence(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        evidence_floor = now - dt.timedelta(hours=7, minutes=59)
        gate = {
            "domains": {
                "raw_retention": {
                    "evidence_floor_generated_at": evidence_floor.isoformat()
                }
            }
        }

        self.assertEqual(
            bounded_permit_expiry(
                gate,
                now,
                dt.timedelta(hours=8),
                dt.timedelta(minutes=30),
            ),
            evidence_floor + dt.timedelta(hours=8),
        )
        with self.assertRaisesRegex(
            ValueError,
            "raw object-store verification is stale",
        ):
            bounded_permit_expiry(
                gate,
                evidence_floor + dt.timedelta(hours=8),
                dt.timedelta(hours=8),
                dt.timedelta(minutes=30),
            )

    def test_retention_requires_an_explicit_raw_domain_evidence_floor(self) -> None:
        with self.assertRaisesRegex(ValueError, "raw retention domain is missing"):
            raw_evidence_time({"raw_retention_ready": True})

    def test_retention_revalidates_evidence_before_each_permit_batch(self) -> None:
        source = RETENTION_TEMPLATE.read_text(encoding="utf-8")
        batch_loop = source.index(
            "for index in range(0, len(candidates), 500):"
        )

        self.assertGreater(
            source.index("current_summary = json.loads", batch_loop),
            batch_loop,
        )
        self.assertGreater(
            source.index("current_report = json.loads", batch_loop),
            batch_loop,
        )
        self.assertGreater(
            source.index("current_gate = json.loads", batch_loop),
            batch_loop,
        )
        self.assertGreater(
            source.index("expires = min(", batch_loop),
            batch_loop,
        )
        self.assertGreater(
            source.index("bounded_permit_expiry(", batch_loop),
            batch_loop,
        )


if __name__ == "__main__":
    unittest.main()
