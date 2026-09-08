"""Keep reader-only corrective deployment scoped and auditable."""
from pathlib import Path
import unittest


PLAYBOOK = Path(__file__).parents[1] / "playbooks/archive_recovery_health.yml"


class ArchiveHealthDeploymentTests(unittest.TestCase):
    def test_verified_unique_rollback_precedes_reader_installation(self):
        source = PLAYBOOK.read_text()
        self.assertIn("not archive_recovery_health_before.results[1].stat.exists", source)
        self.assertIn("archive_recovery_health_saved.stat.checksum == archive_recovery_health_before.results[0].stat.checksum", source)
        self.assertIn("archive_recovery_health_saved.stat.mode == '0600'", source)
        self.assertIn("archive_recovery_health_saved.stat.uid == 0", source)
        self.assertIn("archive_recovery_health_saved.stat.gid == 0", source)
        self.assertLess(source.index("Verify saved bytes"), source.index("Install only the archive health reader"))
        self.assertLess(source.index("Reject a concurrent reader"), source.index("Install only the archive health reader"))
        self.assertIn("unsafe_writes: false", source)

    def test_correction_is_recorded_only_after_a_changed_install(self):
        source = PLAYBOOK.read_text()
        record = source.split("- name: Record corrective deployment", 1)[1].split("- name: Optionally refresh", 1)[0]
        self.assertIn("queue.record_manual_intervention()", record)
        self.assertIn("archive_recovery_health_installed.changed", record)
        self.assertIn("not ansible_check_mode", record)
        self.assertLess(source.index("Install only the archive health reader"), source.index("queue.record_manual_intervention()"))
        for forbidden in ("queue.retry(", "queue.enqueue(", "queue.invalidate(", "queue.tick(", "UPDATE jobs", "recovery_deployment_id"):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("dest: /etc/", source)
        self.assertIn("archive_recovery_health_refresh: false", source)


if __name__ == "__main__":
    unittest.main()
