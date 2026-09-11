"""A busy publication lock cannot manufacture an acceptance failure or sample."""
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).parents[1] / 'roles/object_store_mirror/files'
sys.path.insert(0, str(SOURCE))
import aurora_object_store_evidence as evidence
import aurora_object_store_s3 as reader
spec = importlib.util.spec_from_file_location('acceptance_snapshot_test', SOURCE / 'aurora_object_store_acceptance.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class AcceptanceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = {'recovery_root': str(self.root), 'manifest_root': str(self.root / 'manifests'),
                       'gws_manifest_root': str(self.root / 'gws'), 'recovery_deployment_id': 'test',
                       'jobs': [{'name': 'raw'}, {'name': 'products'}], 'streams': [{'name': 'x'}]}
        self.now = a._time('2026-09-11T03:42:00Z')
        self.previous = {'deployment_id': 'test', 'acceptance_policy_version': 3,
                         'clean_window_started_at': a._iso(self.now - 3600),
                         'updated_at': a._iso(self.now - 300), 'status': 'under_validation',
                         'sample_count': 12, 'manual_intervention_count': 0,
                         'clean_window_hours': 0.9, 'completed_daily_audits': [],
                         'completed_raw_observations': 0, 'failures': []}
        self.path = self.root / 'acceptance.json'
        self.path.write_text(json.dumps(self.previous) + '\n')
        self.coordinator = {'heartbeat_at': a._iso(self.now), 'state': 'running',
                            'manual_intervention_count': 0, 'jobs': {}}
        (self.root / 'status.json').write_text(json.dumps(self.coordinator))
        self.resources = {'used_bytes': 1024, 'max_bytes': 10 * 1024**3,
                          'free_bytes': 100 * 1024**3, 'reserve_bytes': 50 * 1024**3}
        for patcher in (mock.patch.object(reader, 'check_resources', return_value=self.resources),
                        mock.patch('time.time', return_value=self.now)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def generation(self):
        root = Path(self.config['manifest_root'])
        generation = root / 'generations' / ('a' * 32)
        generation.mkdir(parents=True)
        (root / 'latest').symlink_to(generation, target_is_directory=True)
        (root / '.inventory.lock').touch()
        (generation / '.pin.lock').touch()
        comparison = {key: [] for key in ('missing_from_right', 'size_mismatch', 'checksum_mismatch')}
        report = {'generated_at': a._iso(self.now), 'jobs': {
            name: {'verification_id': name, 'evidence_started_at': a._iso(self.now - 600),
                   'source_vs_s3': comparison, 'source_vs_gws': comparison}
            for name in ('raw', 'products')}}
        data = json.dumps(report).encode()
        gate = {'last_generated_at': report['generated_at'], 'report_sha256': hashlib.sha256(data).hexdigest(),
                'raw_retention_ready': True, 'families': {
                    name: {'stable_parity': True} for name in ('raw', 'products')}}
        (generation / 'comparison.json').write_bytes(data)
        (generation / 'verification-gate.json').write_text(json.dumps(gate))
        gws = {'generated_at': a._iso(self.now), 'streams': {'x': {field: 0 for field in (
            'local_missing_count', 'local_mismatch_count', 'gws_missing_count', 'gws_mismatch_count',
            'retention_local_missing_count', 'retention_local_mismatch_count',
            'retention_gws_missing_count', 'retention_gws_mismatch_count')}}}
        (self.root / 'gws/latest').mkdir(parents=True)
        (self.root / 'gws/latest/summary.json').write_text(json.dumps(gws))
        with sqlite3.connect(self.root / 'queue.sqlite') as db:
            db.executescript('CREATE TABLE observations (verification_id TEXT); '
                             'CREATE TABLE daily_audits (id TEXT); '
                             'CREATE TABLE daily_jobs (batch TEXT,job TEXT,verification_id TEXT);')
        return root, gate

    def test_real_publication_lock_preserves_previous_bytes_timestamp_and_credit(self):
        root, _ = self.generation()
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        with (root / '.inventory.lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch('time.sleep') as sleep, mock.patch.object(a.urllib.request, 'urlopen') as fetch:
                state = a.observe(self.config)
        self.assertEqual(state, self.previous)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
        fetch.assert_not_called()
        self.assertEqual(sleep.call_count, 9)

    def test_short_contention_retries_then_samples_bound_evidence_once(self):
        _, gate = self.generation()
        original = evidence.read_snapshot
        calls = []

        @contextmanager
        def sometimes_busy(config):
            calls.append(True)
            if len(calls) == 1:
                raise BlockingIOError('busy')
            with original(config) as snapshot:
                yield snapshot

        public = {'overallLevel': 'green', 'alerts': [], 'updatedAt': a._iso(self.now)}
        with mock.patch.object(evidence, 'read_snapshot', sometimes_busy), mock.patch('time.sleep') as sleep, \
             mock.patch.object(a, 'evaluate_current_gate', return_value=gate), \
             mock.patch.object(a.urllib.request, 'urlopen', return_value=io.StringIO(json.dumps(public))):
            state = a.observe(self.config)
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(0.2)
        self.assertEqual(state['failures'], [])
        self.assertEqual(state['clean_window_started_at'], self.previous['clean_window_started_at'])
        self.assertEqual(state['sample_count'], 13)

    def test_first_deferred_read_does_not_invent_a_record(self):
        self.path.unlink()
        with mock.patch.object(evidence, 'read_snapshot', side_effect=BlockingIOError('busy')), \
             mock.patch('time.sleep'):
            self.assertEqual(a.observe(self.config), {})
        self.assertFalse(self.path.exists())

    def test_long_contention_still_loses_unsampled_credit(self):
        self.previous['updated_at'] = a._iso(self.now - 901)
        self.path.write_text(json.dumps(self.previous))
        with mock.patch.object(evidence, 'read_snapshot', side_effect=BlockingIOError('busy')), \
             mock.patch('time.sleep'):
            state = a.observe(self.config)
        self.assertIsNone(state['clean_window_started_at'])
        self.assertEqual(state['status'], 'under_validation')
        self.assertEqual(state['completed_raw_observations'], 0)
        self.assertEqual(state['completed_daily_audits'], [])

    def test_missing_corrupt_and_unreadable_evidence_still_fail_closed(self):
        for error in (FileNotFoundError(), ValueError(), PermissionError()):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(evidence, 'read_snapshot', side_effect=error) as pin, \
                 mock.patch('time.sleep') as sleep:
                self.path.write_text(json.dumps(self.previous))
                state = a.observe(self.config)
                self.assertIsNone(state['clean_window_started_at'])
                self.assertIn(type(error).__name__, state['failures'][0])
                pin.assert_called_once()
                sleep.assert_not_called()

    def test_known_source_fault_is_not_hidden_by_contention(self):
        self.coordinator['jobs'] = {'raw': {'error_class': 'source_metadata'}}
        (self.root / 'status.json').write_text(json.dumps(self.coordinator))
        with mock.patch.object(evidence, 'read_snapshot', side_effect=BlockingIOError('busy')), \
             mock.patch('time.sleep'):
            state = a.observe(self.config)
        self.assertIsNone(state['clean_window_started_at'])

    def test_block_heartbeat_and_manual_intervention_still_reject_during_contention(self):
        for fault in ('blocked', 'heartbeat', 'intervention', 'counter', 'deployment', 'policy'):
            with self.subTest(fault=fault):
                previous = dict(self.previous)
                coordinator = dict(self.coordinator)
                if fault == 'blocked': coordinator['state'] = 'blocked'
                if fault == 'heartbeat': coordinator['heartbeat_at'] = a._iso(self.now - 901)
                if fault == 'intervention': coordinator['last_manual_intervention_at'] = a._iso(self.now - 10)
                if fault == 'counter': coordinator['manual_intervention_count'] = 1
                if fault == 'deployment': previous['deployment_id'] = 'old-deployment'
                if fault == 'policy': previous['acceptance_policy_version'] = 2
                self.path.write_text(json.dumps(previous))
                (self.root / 'status.json').write_text(json.dumps(coordinator))
                with mock.patch.object(evidence, 'read_snapshot', side_effect=BlockingIOError('busy')), \
                     mock.patch('time.sleep'):
                    state = a.observe(self.config)
                self.assertIsNone(state['clean_window_started_at'])
                self.assertEqual(state['clean_window_hours'], 0)

    def test_resource_exhaustion_is_not_deferred_as_a_publication_failure(self):
        with mock.patch.object(reader, 'check_resources', side_effect=reader.InventoryError('quota', 'resource')), \
             mock.patch.object(evidence, 'read_snapshot') as pin:
            state = a.observe(self.config)
        pin.assert_not_called()
        self.assertIsNone(state['clean_window_started_at'])
        self.assertIn('InventoryError', state['last_failure']['reasons'][0])

    def test_next_successful_read_still_reevaluates_expiry_after_deferral(self):
        _, gate = self.generation()
        with mock.patch.object(evidence, 'read_snapshot', side_effect=BlockingIOError('busy')), \
             mock.patch('time.sleep'):
            a.observe(self.config)
        gate['families']['raw']['stable_parity'] = False
        gate['raw_retention_ready'] = False
        public = {'overallLevel': 'green', 'alerts': [], 'updatedAt': a._iso(self.now)}
        with mock.patch.object(a, 'evaluate_current_gate', return_value=gate) as reevaluate, \
             mock.patch.object(a.urllib.request, 'urlopen', return_value=io.StringIO(json.dumps(public))):
            state = a.observe(self.config)
        reevaluate.assert_called_once()
        self.assertIsNone(state['clean_window_started_at'])
        self.assertIn('raw: two current complete confirmations required', state['failures'])

    def test_malformed_diagnostic_metadata_does_not_prevent_fail_closed_record(self):
        self.previous.update(sample_count='invalid', last_failure=None, failures=['old'], clean_window_started_at=None)
        self.path.write_text(json.dumps(self.previous))
        with mock.patch.object(evidence, 'read_snapshot', side_effect=ValueError()):
            state = a.observe(self.config)
        self.assertEqual(state['sample_count'], 1)
        self.assertIsNone(state['clean_window_started_at'])

    def test_rejection_reason_survives_the_next_clean_sample(self):
        with mock.patch.object(evidence, 'read_snapshot', side_effect=ValueError('never log sensitive details')):
            rejected = a.observe(self.config)
        self.assertEqual(rejected['last_failure']['reasons'], ['acceptance evidence unavailable: ValueError'])
        self.assertEqual(rejected['last_failure']['previous_window_started_at'], self.previous['clean_window_started_at'])
        _, gate = self.generation()
        public = {'overallLevel': 'green', 'alerts': [], 'updatedAt': a._iso(self.now)}
        with mock.patch.object(a, 'evaluate_current_gate', return_value=gate), \
             mock.patch.object(a.urllib.request, 'urlopen', return_value=io.StringIO(json.dumps(public))):
            state = a.observe(self.config)
        self.assertEqual(state['failures'], [])
        self.assertEqual(state['last_failure'], rejected['last_failure'])
        self.assertNotIn('never log', json.dumps(state))


if __name__ == '__main__':
    unittest.main()
