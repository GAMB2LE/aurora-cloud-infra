from pathlib import Path
import unittest


TEMPLATES = (
    Path(__file__).parents[1] / "roles" / "object_store_mirror" / "templates"
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

    def test_recheck_unit_can_persist_confirmation_state(self) -> None:
        source = (
            TEMPLATES / "aurora-object-store-recheck-after-repair.service.j2"
        ).read_text(encoding="utf-8")
        read_write_paths = next(
            line for line in source.splitlines() if line.startswith("ReadWritePaths=")
        )

        self.assertIn("{{ object_store_repair_state_root }}", read_write_paths)

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
