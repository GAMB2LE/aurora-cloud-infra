from pathlib import Path
import unittest


PLAYBOOK = Path(__file__).parents[1] / "playbooks" / "archive_recovery_patch.yml"


class ArchiveRecoveryPatchTests(unittest.TestCase):
    def setUp(self):
        self.source = PLAYBOOK.read_text(encoding="utf-8")

    def test_patch_scope_is_exact_and_never_changes_worker_or_queue_state(self):
        files = self.source.split("archive_recovery_patch_files:\n", 1)[1].split("  tasks:\n", 1)[0]
        self.assertEqual(
            [line.strip().removeprefix("- ") for line in files.splitlines() if line.strip()],
            [
                "/usr/local/lib/aurora-object-store/aurora_object_store_evidence.py",
                "/usr/local/lib/aurora-object-store/aurora_object_store_recovery.py",
                "/etc/aurora-object-store/catalog.json",
            ],
        )
        tasks = self.source.split("  tasks:\n", 1)[1]
        self.assertNotIn("ansible.builtin.systemd", tasks)
        self.assertNotIn("ansible.builtin.service", tasks)
        self.assertNotIn("ansible.builtin.shell", tasks)
        self.assertNotIn("systemctl", tasks)
        self.assertNotIn("queue.sqlite", tasks)
        self.assertEqual(tasks.count("ansible.builtin.command:"), 1)
        self.assertIn("argv: [/usr/bin/mkdir, -m, '0700'", tasks)

    def test_backup_is_unique_restricted_and_verified_before_runtime_writes(self):
        self.assertIn("patch-{{ object_store_recovery_deployment_id }}", self.source)
        self.assertIn("not archive_recovery_patch_existing_backup.stat.exists", self.source)
        self.assertIn("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", self.source)
        self.assertIn("checksum_algorithm: sha256", self.source)
        self.assertIn("item.stat.checksum == archive_recovery_patch_before.results[archive_recovery_patch_index].stat.checksum", self.source)
        self.assertIn("item.stat.mode == '0600'", self.source)
        self.assertIn("item.stat.uid == 0", self.source)
        self.assertIn("item.stat.gid == 0", self.source)
        self.assertLess(
            self.source.index("Verify every saved file against its original"),
            self.source.index("Atomically install only the two patched runtime modules"),
        )
        self.assertLess(
            self.source.index("Reject concurrent catalogue or runtime changes"),
            self.source.index("Atomically install only the two patched runtime modules"),
        )

    def test_catalogue_merge_only_changes_identity_and_preserves_permissions(self):
        update = self.source.split("- name: Atomically update only deployment identity", 1)[1]
        self.assertIn("archive_recovery_live_catalog | combine({'recovery_deployment_id': object_store_recovery_deployment_id})", update)
        self.assertNotIn("archive_recovery_settings", update)
        for field in ("uid", "gid", "mode"):
            self.assertIn("archive_recovery_patch_before.results[2].stat." + field, update)
        self.assertIn("archive_recovery_live_catalog.recovery_enabled | default(false) | bool", self.source)
        self.assertIn("unsafe_writes: false", update)

    def test_check_mode_does_not_create_backups_or_run_commands(self):
        backup = self.source.split("- name: Save and verify restricted rollback files", 1)[1].split("    - name: Atomically install", 1)[0]
        self.assertIn("when: not ansible_check_mode", backup)
        self.assertEqual(backup.count("ansible.builtin.command:"), self.source.count("ansible.builtin.command:"))


if __name__ == "__main__":
    unittest.main()
