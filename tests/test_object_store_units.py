from pathlib import Path
import unittest


TEMPLATES = (
    Path(__file__).parents[1] / "roles" / "object_store_mirror" / "templates"
)
FILES = Path(__file__).parents[1] / "roles" / "object_store_mirror" / "files"
GROUP_VARS = (
    Path(__file__).parents[1] / "inventory" / "group_vars" / "aurora_cloud.yml"
)


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

    def test_recheck_unit_can_persist_confirmation_state(self) -> None:
        source = (
            TEMPLATES / "aurora-object-store-recheck-after-repair.service.j2"
        ).read_text(encoding="utf-8")
        read_write_paths = next(
            line for line in source.splitlines() if line.startswith("ReadWritePaths=")
        )

        self.assertIn("{{ object_store_repair_state_root }}", read_write_paths)

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
        source = (
            Path(__file__).parents[1]
            / "roles"
            / "ass_retention"
            / "templates"
            / "aurora-ass-retention.py.j2"
        ).read_text(encoding="utf-8")

        self.assertIn('"raw_retention_ready"', source)
        self.assertIn('object_gate.get("stable_parity", False)', source)


if __name__ == "__main__":
    unittest.main()
