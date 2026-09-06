"""Keep acceptance-only rollout separate from collection and retention state."""
from pathlib import Path
import unittest


class AcceptanceDeploymentTests(unittest.TestCase):
    def test_focused_patch_preserves_workers_and_configuration(self):
        text = (Path(__file__).parents[1] / 'playbooks' /
                'archive_recovery_acceptance.yml').read_text()
        self.assertNotIn('ansible.builtin.systemd', text)
        self.assertNotIn('notify:', text)
        self.assertNotIn('recovery_deployment_id', text)
        self.assertNotIn('dest: /etc/', text)
        self.assertNotIn('queue.sqlite', text)
        self.assertIn('unsafe_writes: false', text)
        self.assertIn('validate: /usr/bin/python3 -m py_compile %s', text)

    def test_verified_unique_rollback_precedes_module_update(self):
        text = (Path(__file__).parents[1] / 'playbooks' /
                'archive_recovery_acceptance.yml').read_text()
        self.assertIn('not archive_recovery_acceptance_existing.stat.exists', text)
        self.assertIn("argv: [/usr/bin/mkdir, -m, '0700'", text)
        self.assertIn('archive_recovery_acceptance_saved.stat.checksum ==', text)
        self.assertIn("archive_recovery_acceptance_saved.stat.mode == '0600'", text)
        self.assertIn("archive_recovery_acceptance_before.results[0].stat.uid == 0", text)
        self.assertLess(text.index('Verify the saved bytes'),
                        text.index('Atomically install only'))


if __name__ == '__main__':
    unittest.main()
