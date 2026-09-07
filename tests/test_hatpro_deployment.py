import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('hatpro_deploy', ROOT / 'scripts/deploy_hatpro_discovery.py')
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)

LEGACY = '''#!/usr/bin/env bash
source_user=aurora
source_host=100.124.55.22
source_port=22
source_path=/home/aurora/data/hatprog5
source_pattern='HATPROG5-AURORA-ICELAND_*'
destination=/project/aurora/raw/hatprog5
state_file=/var/lib/aurora-cloud/hatpro-sync.last
source_auth=tailscale
ssh_key=/home/aurora/.ssh/key
known_hosts=/home/aurora/.ssh/known_hosts
start_fresh=0
'''
WRAPPER = '''#!/usr/bin/env bash
set -euo pipefail
# A deployment-managed wrapper.
exec /usr/bin/python3 -B /usr/local/lib/aurora-hatpro-discovery.py \\
  --source-user aurora --source-host 100.124.55.22 --source-port 22 \\
  --source-path /home/aurora/data/hatprog5 --source-pattern 'HATPROG5-AURORA-ICELAND_*' \\
  --destination /project/aurora/raw/hatprog5 --state-file /var/lib/aurora-cloud/hatpro-sync.last \\
  --source-auth tailscale --ssh-key /home/aurora/.ssh/key \\
  --known-hosts /home/aurora/.ssh/known_hosts
'''


class HatproDeploymentTests(unittest.TestCase):
    def test_legacy_and_wrapper_bindings_are_identical(self):
        self.assertEqual(deploy.configuration(LEGACY), deploy.configuration(WRAPPER))

    def test_fresh_start_binding_is_preserved(self):
        self.assertEqual(deploy.configuration(LEGACY.replace('start_fresh=0', 'start_fresh=1')),
                         deploy.configuration(WRAPPER + ' --start-fresh'))

    def test_shell_injection_in_legacy_assignment_is_not_executed(self):
        with self.assertRaises(ValueError):
            deploy.configuration(LEGACY.replace('source_user=aurora', 'source_user=aurora; false'))

    def test_missing_or_duplicate_binding_is_rejected(self):
        for source in (WRAPPER.replace('--source-user aurora', ''), WRAPPER + ' --source-host changed'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                deploy.configuration(source)

    def test_noncommissioned_state_path_is_rejected(self):
        with self.assertRaises(ValueError):
            deploy.configuration(LEGACY.replace('/var/lib/aurora-cloud/hatpro-sync.last', '/tmp/cursor'))

    def test_idle_requires_loaded_inactive_units_without_pids_or_jobs(self):
        def result(active='inactive', pid='0', job=''):
            return '\n\n'.join(f'Id={unit}\nLoadState=loaded\nActiveState={active}\nMainPID={pid}\nJob={job}'
                                for unit in deploy.UNITS)
        with mock.patch.object(deploy.subprocess, 'run', return_value=SimpleNamespace(stdout=result())):
            self.assertEqual(set(deploy.idle()), set(deploy.UNITS))
        for values in (('activating', '0', ''), ('inactive', '45', ''), ('inactive', '0', '1234')):
            with mock.patch.object(deploy.subprocess, 'run', return_value=SimpleNamespace(stdout=result(*values))):
                with self.assertRaisesRegex(RuntimeError, 'not idle'):
                    deploy.idle()

    def test_release_label_cannot_escape_rollback_directory(self):
        with mock.patch.object(deploy.os, 'geteuid', return_value=0):
            with self.assertRaises(ValueError):
                deploy.deploy('install', '../../elsewhere', {})

    def test_focused_playbook_does_not_restart_or_install_units(self):
        source = (ROOT / 'playbooks/hatpro_discovery.yml').read_text()
        self.assertNotIn('ansible.builtin.systemd', source)
        self.assertNotIn('daemon_reload:', source)
        self.assertNotIn('state: restarted', source)
        self.assertIn('hatpro_live_context.config.source_host', source)
        self.assertIn('hatpro_live_context.config.state_file', source)
        self.assertIn('preflight', source)


if __name__ == '__main__':
    unittest.main()
