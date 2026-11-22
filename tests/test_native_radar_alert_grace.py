"""Normal hourly delivery is pending; overdue, corrupt and unrelated gaps alert."""
import copy
import datetime as dt
import unittest
from unittest import mock

from test_mirror_verify_active_paths import TEMPLATE as MIRROR_TEMPLATE
import test_mirror_verify_active_paths as mirror_tests
import test_archive_health_presentation as health

operator_status = health.operator_status
settled_source_status = health.settled_source_status

PATH = 'Y2026/M11/D21/rpgfmcw94-aurora_261121_095959_P01_ZEN.LV0.NC'


class NativeRadarAlertGraceTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.fixtures = health.ArchiveHealthPresentationTests()
        source = 'def native_radar_alert_grace' + MIRROR_TEMPLATE.read_text().split('def native_radar_alert_grace', 1)[1].split('\n\ndef settled_entries', 1)[0]
        namespace = {}
        exec(source, namespace)
        self.grace = namespace['native_radar_alert_grace']

    def snapshot(self, age=1800, name='rpgfmcw94', **extra):
        source = {PATH: {'mtime': int(self.now.timestamp()) - age}}
        state = dict(local_missing_count=1, local_mismatch_count=0,
                     gws_missing_count=0, gws_mismatch_count=0)
        state.update(extra)
        state['path_alert_grace'] = self.grace({'name': 'rpgfmcw94'}, source, [PATH], [], int(self.now.timestamp()))
        return dict(generated_at=self.now.isoformat(), gws_available=True, streams={name: state})

    def present(self, summary, now=None):
        now = now or self.now
        settled = settled_source_status(summary, [{'name': n} for n in summary['streams']], now)
        metrics = {**self.fixtures.base_metrics(), **settled['metrics']}
        presentation = operator_status(settled['failures'], metrics, self.fixtures.clean_gate(now), {})
        return settled, presentation

    def test_normal_window_is_pending_without_changing_raw_evidence(self):
        for age in (601, 1800, 2700, 4500, 7199):
            with self.subTest(age=age):
                summary = self.snapshot(age)
                before = copy.deepcopy(summary)
                status, alert = self.present(summary)
                self.assertEqual(status['metrics']['settled_source_local_missing_count'], 1)
                self.assertEqual(status['metrics']['settled_source_alert_local_missing_count'], 0)
                self.assertEqual(status['streams']['rpgfmcw94']['pending_local_missing_count'], 1)
                self.assertEqual(alert['level'], 'green')
                self.assertIn('normal delivery window', alert['detail'])
                self.assertEqual(summary, before)

    def test_overdue_missing_file_alerts(self):
        for age in (7200, 7201, 86400):
            with self.subTest(age=age):
                status, alert = self.present(self.snapshot(age))
                self.assertEqual(status['metrics']['settled_source_alert_local_missing_count'], 1)
                self.assertEqual(alert['level'], 'red')
                self.assertEqual(alert['title'], 'Archive paths are incomplete')

    def test_only_a_new_observation_can_escalate_pending(self):
        for version, transfer in [('native-radar-delivery-v1', 1800), ('native-radar-delivery-v2', 4500)]:
            summary = self.snapshot(2700)
            summary['streams']['rpgfmcw94']['path_alert_grace'].update(policy=version, transfer_grace_seconds=transfer)
            _, alert = self.present(summary, self.now + dt.timedelta(hours=2))
            self.assertEqual(alert['level'], 'green')
            _, stale = self.present(summary, self.now + dt.timedelta(hours=9))
            self.assertIn('overdue', stale['title'])

    def test_mismatch_still_alerts_during_delivery_window(self):
        _, alert = self.present(self.snapshot(local_mismatch_count=1))
        self.assertEqual(alert['level'], 'red')
        self.assertIn('size mismatches', alert['detail'])

    def test_other_streams_receive_no_grace(self):
        _, alert = self.present(self.snapshot(name='hatprog5'))
        self.assertEqual(alert['level'], 'red')
        self.assertIsNone(self.grace({'name': 'hatprog5'}, {}, [], [], int(self.now.timestamp())))

    def test_malformed_or_unbound_grace_fails_closed(self):
        for mutation in ('count', 'duplicate', 'path', 'mtime', 'window'):
            with self.subTest(mutation=mutation):
                summary = self.snapshot()
                grace = summary['streams']['rpgfmcw94']['path_alert_grace']
                if mutation == 'count': grace['raw_missing_counts']['local'] = 2
                if mutation == 'duplicate': grace['pending'] *= 2
                if mutation == 'path': grace['pending'][0]['relative_path'] = '../../other'
                if mutation == 'mtime': grace['pending'][0]['source_mtime'] = True
                if mutation == 'window': grace['transfer_grace_seconds'] = 999999
                status, alert = self.present(summary)
                self.assertEqual(status['metrics']['settled_source_alert_local_missing_count'], 1)
                self.assertEqual(alert['level'], 'red')

    def test_legacy_evidence_does_not_silently_suppress_gaps(self):
        summary = self.snapshot()
        del summary['streams']['rpgfmcw94']['path_alert_grace']
        _, alert = self.present(summary)
        self.assertEqual(alert['level'], 'red')

    def test_gws_pending_is_separate_and_expires(self):
        summary = self.snapshot()
        state = summary['streams']['rpgfmcw94']
        state['gws_missing_count'] = 1
        grace = state['path_alert_grace']
        grace['raw_missing_counts']['gws'] = 1
        grace['pending'].append({**grace['pending'][0], 'destination': 'gws'})
        status, alert = self.present(summary)
        self.assertEqual(status['metrics']['settled_source_gws_missing_count'], 1)
        self.assertEqual(status['metrics']['settled_source_alert_gws_missing_count'], 0)
        self.assertEqual(alert['level'], 'green')
        fresh = self.snapshot(7200, local_missing_count=0, gws_missing_count=1)
        fresh['streams']['rpgfmcw94']['path_alert_grace']['raw_missing_counts'] = {'local': 0, 'gws': 1}
        _, expired = self.present(fresh)
        self.assertIn('configured GWS', expired['detail'])

    def test_full_verifier_preserves_raw_and_retention_counts(self):
        fixture = mirror_tests.ActivePathVerificationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.stream.update(name='rpgfmcw94', gws_relpath='rpgfmcw94',
                              source_pattern='rpgfmcw94-aurora_*')
        ns = mirror_tests.namespace(fixture.config)
        source = {
            PATH: dict(relpath=PATH, size=100, mtime=int(self.now.timestamp()) - 1800, checksum=''),
            PATH.replace('095959', '085959'): dict(
                relpath=PATH.replace('095959', '085959'), size=100,
                mtime=int(self.now.timestamp()) - 7200, checksum=''),
        }
        with mock.patch.dict(ns, {
            'utcnow': lambda: self.now,
            'list_remote_entries': lambda base, target, *args: copy.deepcopy(source) if target == 'instrument@source' else {},
            'list_local_entries': lambda *args: {},
            'service_info': lambda name: dict(service=name, result='success', exit_timestamp=self.now),
        }):
            self.assertEqual(ns['main'](), 0)
        import json
        result = json.loads((fixture.root / 'manifests/latest/summary.json').read_text())['streams']['rpgfmcw94']
        self.assertEqual(result['local_missing_count'], 2)
        self.assertEqual(result['gws_missing_count'], 1)
        self.assertEqual(result['retention_local_missing_count'], 0)
        self.assertEqual(result['retention_gws_missing_count'], 0)
        self.assertEqual(len(result['path_alert_grace']['pending']), 1)
