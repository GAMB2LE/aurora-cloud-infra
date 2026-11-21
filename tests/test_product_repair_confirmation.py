"""Targeted repair confirmation is display evidence, never archive certification."""
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location('product_repair', ROOT / 'roles/object_store_mirror/files/aurora-object-store-repair-from-report.py')
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)
TEMPLATE = (ROOT / 'roles/operations_monitor/templates/aurora-archive-health.py.j2').read_text()
namespace = {}
exec('def verified_product_repairs' + TEMPLATE.split('def verified_product_repairs', 1)[1].split('\n\ndef main', 1)[0], namespace)
confirmed = namespace['verified_product_repairs']
status = namespace['operator_status']


class ProductRepairConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'quicklook.png'
        self.path.write_bytes(b'picture')
        self.now = dt.datetime.now(dt.timezone.utc)
        self.mtime = int((self.now - dt.timedelta(days=2)).timestamp())
        os.utime(self.path, (self.mtime, self.mtime))
        self.job = {'source': str(self.root), 'destination': 'products', 'verification_settle_age': '30h'}
        self.values = {'verification_id': 'audit-one', 'evidence_started_at': (self.now - dt.timedelta(hours=1)).isoformat(),
                       'source': str(self.root), 's3': 's3://bucket/products',
                       'source_vs_s3': {'missing_from_right': ['quicklook.png'], 'size_mismatch': [], 'checksum_mismatch': []}}
        self.catalog = {'remote': 'archive', 'bucket': 'bucket', 'rclone_config': '/restricted/config'}
        self.evidence = {'quicklook.png': {'size': 7, 'mtime': self.mtime}}
        self.response = SimpleNamespace(returncode=0, stdout=json.dumps([{'Path': 'quicklook.png', 'Size': 7, 'IsDir': False}]))

    def receipt(self):
        with mock.patch.object(repair.subprocess, 'run', return_value=self.response) as query:
            receipt = repair.verify_product_findings('products', self.job, self.values, self.catalog, self.evidence)
        self.assertEqual(receipt['state'], 'verified')
        self.assertEqual(query.call_args.args[0][1:3], ['lsjson', 'archive:bucket/products/quicklook.png'])
        return receipt

    def report_and_result(self):
        return {'jobs': {'products': copy.deepcopy(self.values)}}, {'jobs': [{'job': 'products', 'product_verification': self.receipt()}]}

    def presentation(self, confirmations, **changes):
        metrics = {'streams_gws_issue_count': 0, 'object_store_all_missing_count': 1,
                   'object_store_all_mismatch_count': 0, 'gws_all_missing_count': 0, 'gws_all_mismatch_count': 0}
        metrics.update(changes.pop('metrics', {}))
        gate = {'domains': {'raw_retention': {'clean': True, 'stable_parity': True,
                         'evidence_floor_generated_at': self.now.isoformat()}}}
        args = dict(failures=['object_store_all_missing=1', 'object_store_stable_parity=false'],
                    metrics=metrics, gate=gate, progress={'state': 'running'}, product_repairs=confirmations)
        args.update(changes)
        return status(**args)

    def test_verified_repair_is_amber_without_changing_gate_or_metrics(self):
        report, result = self.report_and_result()
        original = copy.deepcopy(report)
        confirmations = confirmed(report, result)
        self.assertEqual(confirmations, {'missing': 1, 'mismatch': 0})
        display = self.presentation(confirmations)
        self.assertEqual(display['level'], 'amber')
        self.assertEqual(display['title'], 'Repair verified; full audit continuing')
        self.assertEqual(report, original)
        self.assertNotIn('stable_parity', confirmations)
        for state in ('queued', 'retry_wait'):
            self.assertEqual(self.presentation(confirmations, progress={'state': state})['level'], 'amber')

    def test_missing_wrong_size_wrong_name_or_timeout_do_not_confirm(self):
        for response in (SimpleNamespace(returncode=1, stdout=''),
                         SimpleNamespace(returncode=0, stdout='[]'),
                         SimpleNamespace(returncode=0, stdout='not json'),
                         SimpleNamespace(returncode=0, stdout=json.dumps([{'Path': 'quicklook.png', 'Size': 8, 'IsDir': False}])),
                         SimpleNamespace(returncode=0, stdout=json.dumps([{'Path': 'another.png', 'Size': 7, 'IsDir': False}]))):
            with self.subTest(response=response), mock.patch.object(repair.subprocess, 'run', return_value=response):
                self.assertEqual(repair.verify_product_findings('products', self.job, self.values, self.catalog, self.evidence), {'state': 'unverified'})
        with mock.patch.object(repair.subprocess, 'run', side_effect=subprocess.TimeoutExpired('query', 20)):
            self.assertEqual(repair.verify_product_findings('products', self.job, self.values, self.catalog, self.evidence)['state'], 'unverified')

    def test_source_change_during_or_after_query_invalidates_confirmation(self):
        def changing(*args, **kwargs):
            self.path.write_bytes(b'changed')
            os.utime(self.path, (self.mtime, self.mtime))
            return self.response
        with mock.patch.object(repair.subprocess, 'run', side_effect=changing):
            self.assertEqual(repair.verify_product_findings('products', self.job, self.values, self.catalog, self.evidence)['state'], 'unverified')
        report, result = self.report_and_result()
        self.path.write_bytes(b'newdata')
        os.utime(self.path, (self.mtime, self.mtime))
        self.assertEqual(confirmed(report, result), {})

    def test_new_audit_expiry_bad_receipts_and_partial_coverage_fail_closed(self):
        report, result = self.report_and_result()
        cases = []
        newer = copy.deepcopy(report); newer['jobs']['products']['verification_id'] = 'audit-two'; cases.append((newer, result))
        clean = copy.deepcopy(report); clean['jobs']['products']['source_vs_s3']['missing_from_right'] = []
        self.assertEqual(confirmed(clean, result), {})
        expired = copy.deepcopy(report); expired['jobs']['products']['evidence_started_at'] = (self.now - dt.timedelta(hours=37)).isoformat(); cases.append((expired, result))
        partial = copy.deepcopy(report); partial['jobs']['products']['source_vs_s3']['missing_from_right'].append('other.png'); cases.append((partial, result))
        checksum = copy.deepcopy(report); checksum['jobs']['products']['source_vs_s3']['checksum_mismatch'] = ['quicklook.png']; cases.append((checksum, result))
        wrong_destination = copy.deepcopy(report); wrong_destination['jobs']['products']['s3'] = 's3://another/products'; cases.append((wrong_destination, result))
        raw = copy.deepcopy(report); raw['jobs']['raw'] = raw['jobs'].pop('products'); cases.append((raw, result))
        cases.extend([(report, {}), (report, {'jobs': [None]})])
        for changed_report, changed_result in cases:
            with self.subTest(report=changed_report, result=changed_result):
                self.assertEqual(confirmed(changed_report, changed_result), {})
                self.assertEqual(self.presentation(confirmed(changed_report, changed_result))['level'], 'red')

    def test_unrelated_red_conditions_are_never_suppressed(self):
        report, result = self.report_and_result()
        confirmation = confirmed(report, result)
        changes = [dict(metrics={'gws_all_missing_count': 1}),
                   dict(metrics={'object_store_all_missing_count': 2}),
                   dict(metrics={'settled_source_alert_local_missing_count': 1}),
                   dict(metrics={'archive_delivery_oldest_pending_age_minutes': 121}),
                   dict(recovery={'active_alert': True}), dict(gate={}),
                   dict(failures=['archive_service_unhealthy=copy.service']),
                   dict(progress={'state': 'failed'})]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(self.presentation(confirmation, **change)['level'], 'red')

    def test_size_mismatch_can_be_resolved_but_raw_and_checksums_cannot(self):
        self.values['source_vs_s3']['missing_from_right'] = []
        self.values['source_vs_s3']['size_mismatch'] = ['quicklook.png']
        report, result = self.report_and_result()
        self.assertEqual(confirmed(report, result), {'missing': 0, 'mismatch': 1})
        with mock.patch.object(repair.subprocess, 'run') as query:
            self.assertEqual(repair.verify_product_findings('raw', self.job, self.values, self.catalog, self.evidence)['state'], 'unverified')
            self.values['source_vs_s3']['checksum_mismatch'] = ['quicklook.png']
            self.assertEqual(repair.verify_product_findings('products', self.job, self.values, self.catalog, self.evidence)['state'], 'unverified')
            query.assert_not_called()

    def test_copy_success_is_followed_by_targeted_verification(self):
        with mock.patch.object(repair.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0), self.response]) as calls:
            result = repair.repair_job('products', self.job, self.values, self.catalog, False, self.evidence)
        self.assertEqual(result['product_verification']['state'], 'verified')
        self.assertEqual([call.args[0][1] for call in calls.call_args_list], ['copy', 'lsjson'])

    def test_verify_only_does_not_copy_or_touch_recovery_queue(self):
        (self.root / 'products-local.tsv').write_text('relative_path\tsize\tmtime\nquicklook.png\t7\t' + str(self.mtime) + '\n')
        snapshot = SimpleNamespace(report={'generated_at': 'report-one', 'jobs': {'products': self.values}}, path=self.root)
        reader = mock.MagicMock()
        reader.return_value.__enter__.return_value = snapshot
        args = SimpleNamespace(verify_only=True, dry_run=False, job=['products'], result=self.root / 'result.json')
        catalog = dict(self.catalog, jobs=[dict(self.job, name='products')])
        with mock.patch.dict('sys.modules', {'aurora_object_store_evidence': SimpleNamespace(read_snapshot=reader)}), \
                mock.patch.object(repair.subprocess, 'run', return_value=self.response) as calls:
            result = repair.repair_latest(args, catalog)
        self.assertTrue(result['verify_only'])
        self.assertEqual(result['jobs'][0]['ready'], 0)
        self.assertEqual([call.args[0][1] for call in calls.call_args_list], ['lsjson'])
        self.assertEqual(json.loads(args.result.read_text()), result)

    def test_partial_repair_and_escaped_sources_never_confirm(self):
        self.values['source_vs_s3']['missing_from_right'].append('absent.png')
        with mock.patch.object(repair.subprocess, 'run') as query:
            self.assertEqual(repair.verify_product_findings('products', self.job, self.values, self.catalog, self.evidence)['state'], 'unverified')
            self.values['source_vs_s3']['missing_from_right'] = ['../quicklook.png']
            self.assertEqual(repair.verify_product_findings('products', self.job, self.values, self.catalog, self.evidence)['state'], 'unverified')
            self.values['source_vs_s3']['missing_from_right'] = ['linked.png']
            (self.root / 'linked.png').symlink_to(self.path)
            self.assertEqual(repair.verify_product_findings('products', self.job, self.values, self.catalog, self.evidence)['state'], 'unverified')
            query.assert_not_called()


if __name__ == '__main__':
    unittest.main()
