"""Exercise the focused publisher without production services or credentials."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock


PLAYBOOK = Path(__file__).parents[1] / 'playbooks/archive_source_timestamp_guard.yml'


def load_publisher():
    text = PLAYBOOK.read_text()
    code = textwrap.dedent(text.split('    archive_timestamp_guard_code: |\n', 1)[1].split('\n  tasks:', 1)[0])
    namespace = {'__name__': 'deployment_test'}
    exec(compile(code, str(PLAYBOOK), 'exec'), namespace)
    return namespace


class SourceTimestampDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ns = load_publisher()
        self.ns['ROOT_UID'] = os.geteuid()
        self.ns['ROOT_GID'] = os.getgid()
        self.catalog = self.root / 'catalog.json'
        self.backups = self.root / 'backups'
        self.backups.mkdir(mode=0o700)
        self.stage = self.root / 'stage'
        self.stage.mkdir(mode=0o700)
        self.targets = tuple(str(self.root / Path(path).name) for path in self.ns['TARGETS'])
        self.ns.update(CATALOG=self.catalog, BACKUPS=self.backups, TARGETS=self.targets)
        self.old = {}
        for index, target in enumerate(self.targets):
            path = Path(target)
            data = f'# previous {index}\n'.encode()
            path.write_bytes(data)
            path.chmod(0o755 if index % 2 == 0 else 0o644)
            self.old[target] = (data, path.stat().st_mode, path.stat().st_uid, path.stat().st_gid)
            (self.stage / path.name).write_bytes(b'raise RuntimeError("compile, never execute")\n')
        self.manifests = self.root / 'manifests'
        self.recovery = self.manifests / 'recovery'
        self.recovery.mkdir(parents=True)
        for path in (self.manifests / '.inventory.lock', self.recovery / 'worker-raw.lock', self.recovery / 'worker-products.lock'):
            path.touch()
        generation = self.manifests / 'generation-before'
        generation.mkdir()
        for filename in ('comparison.json', 'verification-gate.json'):
            (generation / filename).write_text('{}\n')
        (self.manifests / 'latest').symlink_to(generation, target_is_directory=True)
        self.config = {'recovery_enabled': True, 'recovery_deployment_id': 'unchanged',
                       'manifest_root': str(self.manifests), 'recovery_root': str(self.recovery),
                       'jobs': [{'name': name} for name in ('raw', 'products', 'products-wxcam',
                                'model-evaluation', 'menapia-flight-manifests', 'manifests')]}
        self.catalog.write_text(json.dumps(self.config))
        self.catalog_bytes = self.catalog.read_bytes()
        self.sha = hashlib.sha256(self.catalog_bytes).hexdigest()
        with sqlite3.connect(self.recovery / 'queue.sqlite') as db:
            db.execute('CREATE TABLE jobs (name TEXT, state TEXT)')
            db.executemany('INSERT INTO jobs VALUES (?,?)', [(j['name'], 'idle') for j in self.config['jobs']])
            db.execute('CREATE TABLE repair_requests (job TEXT, completed INTEGER)')
        self.busy = None
        patcher = mock.patch.object(subprocess, 'run', side_effect=self.systemctl)
        self.commands = patcher.start()
        self.addCleanup(patcher.stop)

    def systemctl(self, argv, **kwargs):
        self.assertEqual(argv[:2], ['/bin/systemctl', 'show'])
        blocks = ['Id=' + name + '\nLoadState=loaded\nActiveState=' +
                  ('activating' if name == self.busy else 'inactive') + '\nJob=0'
                  for name in argv[2:-1]]
        return subprocess.CompletedProcess(argv, 0, '\n\n'.join(blocks) + '\n', '')

    def deploy(self, mode='install', label='test-release'):
        return self.ns['deploy'](mode, label, self.sha, str(self.stage))

    def assert_unchanged(self):
        for path, expected in self.old.items():
            info = Path(path).stat()
            self.assertEqual((Path(path).read_bytes(), info.st_mode, info.st_uid, info.st_gid), expected)
        self.assertEqual(self.catalog.read_bytes(), self.catalog_bytes)

    def test_exact_allowlist_and_no_service_or_configuration_mutations(self):
        ns = load_publisher()
        self.assertEqual(len(ns['TARGETS']), 6)
        self.assertEqual(set(ns['TARGETS']), {
            '/usr/local/bin/aurora-object-store-inventory',
            '/usr/local/lib/aurora-object-store/aurora_object_store_inventory.py',
            '/usr/local/lib/aurora-object-store/aurora_object_store_recovery.py',
            '/usr/local/bin/aurora-object-store-repair-from-report',
            '/usr/local/lib/aurora-object-store/aurora_object_store_acceptance.py',
            '/usr/local/bin/aurora-archive-health'})
        text = PLAYBOOK.read_text()
        for forbidden in ('ansible.builtin.systemd', 'ansible.builtin.service', 'notify:', 'daemon_reload',
                          "'UPDATE ", "'INSERT ", "'DELETE ", 'py_compile'):
            self.assertNotIn(forbidden, text)
        self.assertIn('when: not ansible_check_mode', text)
        self.assertEqual(text.count('validate: /usr/bin/python3 -c "import pathlib, sys; compile('), 2)

    def test_preflight_is_read_only_and_install_preserves_evidence_identity_permissions(self):
        db_before = (self.recovery / 'queue.sqlite').read_bytes()
        pointer = os.readlink(self.manifests / 'latest')
        self.assertEqual(self.deploy('preflight')['state'], 'idle')
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assert_unchanged()
        result = self.deploy()
        self.assertEqual(result['state'], 'installed')
        backup = Path(result['rollback'])
        record = json.loads((backup / 'before.json').read_text())
        self.assertEqual(set(record['files']), set(self.targets))
        self.assertEqual(record['catalog']['sha256'], self.sha)
        for path, old in self.old.items():
            self.assertEqual(Path(path).read_bytes(), (self.stage / Path(path).name).read_bytes())
            info = Path(path).stat()
            self.assertEqual((info.st_mode, info.st_uid, info.st_gid), old[1:])
            saved = backup / (Path(path).name + '.before')
            self.assertEqual(saved.read_bytes(), old[0])
            self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual(self.catalog.read_bytes(), self.catalog_bytes)
        self.assertEqual((self.recovery / 'queue.sqlite').read_bytes(), db_before)
        self.assertEqual(os.readlink(self.manifests / 'latest'), pointer)
        self.assertEqual(list(self.root.rglob('__pycache__')), [])

    def test_busy_workers_repair_retention_and_coordinator_defer_before_backups(self):
        for unit in ('aurora-object-store-recovery.service', 'aurora-object-store-repair.service',
                     'aurora-ass-retention.service', 'aurora-object-store-recovery-worker@raw.service'):
            with self.subTest(unit=unit):
                self.busy = unit
                with self.assertRaisesRegex(RuntimeError, 'not idle'):
                    self.deploy()
                self.assert_unchanged()
                self.assertEqual(list(self.backups.iterdir()), [])

    def test_unfinished_queue_or_repair_defers_without_live_writes(self):
        with sqlite3.connect(self.recovery / 'queue.sqlite') as db:
            db.execute("UPDATE jobs SET state='launching' WHERE name='raw'")
        with self.assertRaisesRegex(RuntimeError, 'unfinished recovery'):
            self.deploy()
        with sqlite3.connect(self.recovery / 'queue.sqlite') as db:
            db.execute("UPDATE jobs SET state='idle'")
            db.execute("INSERT INTO repair_requests VALUES ('raw',0)")
        with self.assertRaisesRegex(RuntimeError, 'unfinished recovery'):
            self.deploy()
        self.assert_unchanged()

    def test_lane_lock_contention_defers_without_waiting(self):
        import fcntl
        with (self.recovery / 'worker-products.lock').open('rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.deploy()
        self.assert_unchanged()

    def test_pending_systemd_job_is_not_an_idle_boundary(self):
        original = self.systemctl
        def pending(argv, **kwargs):
            result = original(argv, **kwargs)
            result.stdout = result.stdout.replace('Job=0', 'Job=123', 1)
            return result
        self.commands.side_effect = pending
        with self.assertRaisesRegex(RuntimeError, 'not idle'):
            self.deploy()
        self.assert_unchanged()
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_changed_catalogue_is_rejected_before_backup_or_live_writes(self):
        self.catalog.write_text(json.dumps({**self.config, 'recovery_deployment_id': 'concurrent-change'}))
        changed = self.catalog.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Catalogue changed'):
            self.deploy()
        self.assertEqual(self.catalog.read_bytes(), changed)
        self.assertEqual(list(self.backups.iterdir()), [])
        for target, before in self.old.items():
            self.assertEqual(Path(target).read_bytes(), before[0])

    def test_invalid_python_and_disagreeing_inventory_copies_never_publish(self):
        (self.stage / Path(self.targets[2]).name).write_text('def invalid(:\n')
        with self.assertRaises(SyntaxError):
            self.deploy()
        self.assertEqual(list(self.backups.iterdir()), [])
        (self.stage / Path(self.targets[0]).name).write_text('# different inventory\n')
        with self.assertRaisesRegex(RuntimeError, 'identical code'):
            self.deploy()
        self.assert_unchanged()

    def test_unique_backup_and_regular_file_guards(self):
        (self.backups / 'source-timestamp-test-release').mkdir()
        with self.assertRaisesRegex(RuntimeError, 'existing rollback'):
            self.deploy()
        with self.assertRaisesRegex(RuntimeError, 'safe release'):
            self.deploy(label='../escape')
        target = Path(self.targets[0])
        target.unlink()
        target.symlink_to(self.stage / target.name)
        with self.assertRaisesRegex(RuntimeError, 'regular file'):
            self.deploy(label='different')

    def test_corrupt_backup_fails_before_live_replacement(self):
        original = self.ns['exclusive']
        def corrupt(path, data):
            original(path, b'corrupt' if str(path).endswith('.before') else data)
        with mock.patch.dict(self.ns, exclusive=corrupt):
            with self.assertRaisesRegex(RuntimeError, 'Rollback verification failed'):
                self.deploy()
        self.assert_unchanged()

    def test_second_idle_boundary_check_prevents_publication_after_staging_race(self):
        original = self.ns['idle']
        calls = []
        def busy_second(config):
            calls.append(True)
            if len(calls) == 2:
                self.busy = 'aurora-object-store-recovery-worker@products.service'
            return original(config)
        with mock.patch.dict(self.ns, idle=busy_second):
            with self.assertRaisesRegex(RuntimeError, 'not idle'):
                self.deploy()
        self.assert_unchanged()
        self.assertTrue((self.backups / 'source-timestamp-test-release/before.json').is_file())

    def test_partial_install_failure_restores_original_bytes_and_permissions(self):
        original = self.ns['replace']
        calls = []
        def fail_third(path, data, metadata):
            calls.append(path)
            if len(calls) == 3:
                raise OSError('injected publication failure')
            original(path, data, metadata)
        with mock.patch.dict(self.ns, replace=fail_third):
            with self.assertRaisesRegex(OSError, 'injected publication failure'):
                self.deploy()
        self.assert_unchanged()
        self.assertFalse((self.backups / 'source-timestamp-test-release/installed.json').exists())


if __name__ == '__main__':
    unittest.main()
