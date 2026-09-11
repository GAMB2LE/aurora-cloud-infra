"""Keep acceptance-only rollout separate from collection and retention state."""
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


VALIDATE_COMMAND = '''/usr/bin/python3 -c "import pathlib, sys; compile(pathlib.Path(sys.argv[1]).read_bytes(), sys.argv[1], 'exec')" "%s"'''


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
        self.assertIn('validate: ' + VALIDATE_COMMAND, text)

    def test_focused_validators_check_syntax_without_bytecode_or_execution(self):
        for playbook in ('archive_recovery_acceptance.yml', 'archive_recovery_health.yml'):
            text = (Path(__file__).parents[1] / 'playbooks' / playbook).read_text()
            self.assertIn('validate: ' + VALIDATE_COMMAND, text)
            self.assertNotIn('py_compile', text)
            for source, valid in ((b'raise RuntimeError("must not execute")\n', True),
                                  (b'def invalid(:\n', False)):
                with self.subTest(playbook=playbook, valid=valid), tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / 'staged source.py'
                    path.write_bytes(source)
                    # Ansible substitutes the staged path and splits argv;
                    # no shell processes the command or its quoted argument.
                    command = shlex.split(VALIDATE_COMMAND % path)
                    command[0] = sys.executable
                    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode == 0, valid, result.stderr)
                    self.assertEqual(path.read_bytes(), source)
                    self.assertEqual(list(Path(temporary).iterdir()), [path])

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

    def test_corrective_install_records_one_intervention_without_restarting_work(self):
        text = (Path(__file__).parents[1] / 'playbooks' /
                'archive_recovery_acceptance.yml').read_text()
        self.assertIn('queue.record_manual_intervention()', text)
        self.assertIn('archive_recovery_acceptance_installed.changed', text)
        self.assertIn('- not ansible_check_mode', text)
        self.assertLess(text.index('Atomically install only'), text.index('queue.record_manual_intervention()'))
        self.assertNotIn('queue.retry(', text)
        self.assertNotIn('queue.enqueue(', text)
        self.assertNotIn('queue.invalidate(', text)


if __name__ == '__main__':
    unittest.main()
