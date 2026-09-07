from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import sys
import unittest
import uuid
from unittest import mock

SCRIPT=Path(__file__).parents[1]/'roles/object_store_mirror/files/aurora_object_store_recovery.py'
SPEC=importlib.util.spec_from_file_location('recovery_test',SCRIPT)
recovery=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
sys.path.insert(0,str(SCRIPT.parent))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.now=100000.0
        self.config={'manifest_root':self.tmp.name,'jobs':[{'name':n,'source':'/source/'+n,'destination':n} for n in ['raw','products','manifests']], 'recovery_retry_delays_seconds':[15,30,60], 'gws_hosts':['gws-a','gws-b','gws-c']}
        self.q=recovery.Queue(self.config,clock=lambda:self.now)

    def tearDown(self):
        self.q.close()
        self.tmp.cleanup()

    def evidence(self,epoch,clean=True,stable=True):
        values={'verification_id':epoch['verification_id'],'verified_at':recovery.iso(epoch['evidence_started_at'])}
        return {'jobs':{epoch['name']:values}}, {'report_sha256':'0'*64,'families':{epoch['name']:{'clean':clean,'stable_parity':stable}}}

    def test_daily_requires_all_fresh_families_and_survives_reopen(self):
        self.q.enqueue(['raw'])
        old=self.q.claim('raw')
        self.now+=1
        self.q.enqueue(daily=True,batch_id='daily')
        self.q.complete(old,*self.evidence(old))
        self.assertEqual(self.q.row('raw')['state'],'queued')
        for name in self.q.jobs:
            self.now+=1
            epoch=self.q.claim(name)
            self.q.complete(epoch,*self.evidence(epoch))
        self.q.close()
        self.q=recovery.Queue(self.config,clock=lambda:self.now)
        self.assertEqual(self.q.status()['daily_audits']['daily']['completed_at'],recovery.iso(self.now))

    def test_retry_only_failed_family_and_preserves_epoch(self):
        self.q.enqueue()
        epoch=self.q.claim('products')
        self.q.fail(epoch,RuntimeError('HTTP 504'))
        first=self.q.row('products')
        self.q.enqueue(['products'])
        self.assertEqual(self.q.row('products')['next_retry_at'],first['next_retry_at'])
        self.assertIsNone(self.q.claim('products'))
        self.now=first['next_retry_at']+1
        resumed=self.q.claim('products')
        self.assertEqual(resumed['verification_id'],epoch['verification_id'])
        self.assertEqual(resumed['evidence_started_at'],epoch['evidence_started_at'])
        self.assertEqual(self.q.row('raw')['state'],'queued')

    def test_source_metadata_fault_survives_retries_interruptions_and_launch_errors(self):
        fault=recovery.load_inventory().SourceMetadataError('camera.jpg has invalid timestamp')
        self.q.enqueue(['raw'])
        epoch=self.q.claim('raw')
        self.q.fail(epoch,fault,restart_epoch=True)
        for event in ('transient','defer','interruption','launch','manual_retry'):
            self.now=self.q.row('raw')['next_retry_at']+1
            if event=='launch':
                self.q.launch_ready(lambda name: (_ for _ in ()).throw(RuntimeError('launch failed')))
            elif event=='manual_retry':
                self.q.retry('raw')
            else:
                epoch=self.q.claim('raw')
                self.assertEqual(epoch['error_class'],'source_metadata')
                if event=='transient':self.q.fail(epoch,RuntimeError('gateway timeout'))
                elif event=='defer':self.q.defer(epoch)
                else:self.q.recover_workers(lambda name:False)
            self.assertEqual(self.q.row('raw')['error_class'],'source_metadata')
            self.assertTrue(self.q.row('raw')['last_error'].startswith(str(fault)))
        self.now=self.q.row('raw')['next_retry_at']+1
        epoch=self.q.claim('raw')
        self.q.complete(epoch,*self.evidence(epoch))
        self.assertIsNone(self.q.row('raw')['error_class'])
        self.assertIsNone(self.q.row('raw')['last_error'])

    def test_sticky_source_fault_keeps_a_later_permanent_failure_explanation(self):
        self.q.enqueue(['raw'])
        epoch=self.q.claim('raw')
        self.q.fail(epoch,recovery.load_inventory().SourceMetadataError('camera timestamp invalid'),restart_epoch=True)
        self.now=self.q.row('raw')['next_retry_at']+1
        epoch=self.q.claim('raw')
        error=RuntimeError('GWS credentials rejected')
        error.error_class='auth'
        self.q.fail(epoch,error)
        row=self.q.row('raw')
        self.assertEqual(row['state'],'blocked')
        self.assertEqual(row['error_class'],'source_metadata')
        self.assertIn('camera timestamp invalid',row['last_error'])
        self.assertIn('latest attempt (auth): GWS credentials rejected',row['last_error'])

    def test_repair_source_metadata_fault_fences_old_audit_without_postponing_retries(self):
        self.q.enqueue(['raw'])
        epoch=self.q.claim('raw')
        fault=recovery.load_inventory().SourceMetadataError('invalid source timestamp')
        self.q.source_metadata_failed('raw',fault)
        first=self.q.row('raw')
        self.assertIsNone(first['verification_id'])
        self.assertEqual(first['generation'],epoch['generation']+1)
        self.assertEqual(first['remaining'],2)
        self.assertEqual(first['state'],'retry_wait')
        with self.assertRaises(RuntimeError):self.q.validate(epoch)
        self.now+=1
        self.q.source_metadata_failed('raw',fault)
        self.assertEqual(self.q.row('raw')['generation'],first['generation'])
        self.assertEqual(self.q.row('raw')['next_retry_at'],first['next_retry_at'])


    def test_backlogged_daily_cycles_require_distinct_observations(self):
        self.q.enqueue(daily=True,batch_id='day-one')
        self.now+=86400
        self.q.enqueue(daily=True,batch_id='day-two')
        for name in self.q.jobs:
            first=self.q.claim(name)
            self.q.complete(first,*self.evidence(first))
        batches=self.q.status()['daily_audits']
        self.assertIsNotNone(batches['day-one']['completed_at'])
        self.assertIsNone(batches['day-two']['completed_at'])
        for name in self.q.jobs:
            second=self.q.claim(name)
            self.q.complete(second,*self.evidence(second))
        batches=self.q.status()['daily_audits']
        self.assertIsNotNone(batches['day-two']['completed_at'])
        for name in self.q.jobs:
            self.assertNotEqual(batches['day-one']['jobs'][name],batches['day-two']['jobs'][name])

    def test_manual_interventions_are_persistent_acceptance_evidence(self):
        self.q.record_manual_intervention()
        self.now += 60
        self.q.record_manual_intervention()
        status = self.q.status()
        self.assertEqual(status['manual_intervention_count'], 2)
        self.assertEqual(status['last_manual_intervention_at'], recovery.iso(self.now))

    def test_changed_gws_stream_configuration_restarts_checkpoint(self):
        self.q.enqueue(['raw'])
        epoch = self.q.claim('raw')
        self.q.fail(epoch, RuntimeError('504'))
        self.now = self.q.row('raw')['next_retry_at'] + 1
        self.config['streams'] = [{'name': 'new-stream', 'prefix': 'new-prefix'}]
        resumed = self.q.claim('raw')
        self.assertNotEqual(resumed['verification_id'], epoch['verification_id'])

    def test_next_retry_ignores_finished_jobs(self):
        self.q.enqueue(['raw'])
        epoch = self.q.claim('raw')
        self.q.fail(epoch, RuntimeError('504'))
        self.assertIsNotNone(self.q.status()['next_retry_at'])
        self.now = self.q.row('raw')['next_retry_at'] + 1
        resumed = self.q.claim('raw')
        self.q.complete(resumed, *self.evidence(resumed))
        self.assertIsNone(self.q.status()['next_retry_at'])

    def test_orphan_cleanup_preserves_live_recent_and_unknown_paths(self):
        self.q.enqueue(['raw'])
        epoch=self.q.claim('raw')
        identifiers=[epoch['verification_id'],str(uuid.uuid4()),str(uuid.uuid4()),'not-a-checkpoint']
        for index, identifier in enumerate(identifiers):
            path=self.q.root/'epochs'/identifier
            path.mkdir(parents=True)
            stamp=self.now-16*3600 if index != 2 else self.now
            os.utime(path,(stamp,stamp))
        self.assertEqual(recovery.cleanup_orphan_epochs(self.q),[identifiers[1]])
        self.assertTrue((self.q.root/'epochs'/identifiers[0]).exists())
        self.assertTrue((self.q.root/'epochs'/identifiers[2]).exists())
        self.assertTrue((self.q.root/'epochs'/identifiers[3]).exists())

    def test_completed_product_gws_scan_is_reused_within_epoch(self):
        self.config['recovery_free_reserve_bytes']=0
        self.q.enqueue(['products'])
        epoch=self.q.claim('products')
        directory=self.q.root/'epochs'/epoch['verification_id']
        directory.mkdir(parents=True)
        recovery.atomic_json(directory/'epoch.json',{'source_sha256':'frozen'})
        inventory=mock.Mock()
        inventory.gws_inventory.return_value={'a':{'relative_path':'a','size':2}}
        first=recovery.collect_gws(self.config,self.q.jobs['products'],epoch,directory,inventory,self.q)
        second=recovery.collect_gws(self.config,self.q.jobs['products'],epoch,directory,inventory,self.q)
        self.assertEqual(first,second)
        inventory.gws_inventory.assert_called_once()
        recovery.atomic_json(directory/'gws.json',{})
        with self.assertRaises(recovery.RestartEpochError):
            recovery.collect_gws(self.config,self.q.jobs['products'],epoch,directory,inventory,self.q)

    def test_failed_gws_collection_leaves_no_reusable_checkpoint(self):
        self.q.enqueue(['products'])
        epoch=self.q.claim('products')
        directory=self.q.root/'epochs'/epoch['verification_id']
        directory.mkdir(parents=True)
        inventory=mock.Mock()
        inventory.gws_inventory.side_effect=RuntimeError('gateway unavailable')
        with self.assertRaises(RuntimeError):
            recovery.collect_gws(self.config,self.q.jobs['products'],epoch,directory,inventory,self.q)
        self.assertFalse((directory/'gws.json').exists())

    def test_gws_attempt_uses_one_rotating_host_without_nested_retries(self):
        self.q.enqueue(['products'])
        epoch=self.q.claim('products')
        directory=self.q.root/'epochs'/epoch['verification_id']
        directory.mkdir(parents=True)
        inventory=mock.Mock()
        inventory.gws_inventory.side_effect=RuntimeError('Connection timed out with private detail')
        self.config.update(gws_inventory_attempts=3,gws_inventory_retry_delay_seconds=15)
        for count,host in enumerate(['gws-a','gws-b','gws-c','gws-a']):
            with self.subTest(attempt=count):
                with self.assertRaisesRegex(RuntimeError,'coordinator will retry another host') as caught:
                    recovery.collect_gws(self.config,self.q.jobs['products'],dict(epoch,attempt_count=count),directory,inventory,self.q)
                cfg=inventory.gws_inventory.call_args.args[0]
                self.assertEqual(cfg['gws_hosts'],[host])
                self.assertEqual(cfg['gws_inventory_attempts'],1)
                self.assertEqual(cfg['gws_inventory_retry_delay_seconds'],0)
                self.assertEqual(caught.exception.error_class,'transient')
                self.assertNotIn('private detail',str(caught.exception))
        self.assertEqual(self.config['gws_inventory_attempts'],3)
        self.assertEqual(self.config['gws_hosts'],['gws-a','gws-b','gws-c'])

    def test_gws_authentication_and_configuration_failures_are_sanitized(self):
        self.q.enqueue(['products'])
        epoch=self.q.claim('products')
        directory=self.q.root/'epochs'/epoch['verification_id']
        directory.mkdir(parents=True)
        cases=[('someuser@secret-host: Permission denied (publickey).','auth'),
               ('Host key verification failed. secret-host','config'),
               ('REMOTE HOST IDENTIFICATION HAS CHANGED! secret-host','config'),
               ('Load key /private/path: invalid format','config')]
        for message,category in cases:
            with self.subTest(category=category,message=message):
                inventory=mock.Mock()
                inventory.gws_inventory.side_effect=RuntimeError(message)
                with self.assertRaises(RuntimeError) as caught:
                    recovery.collect_gws(self.config,self.q.jobs['products'],epoch,directory,inventory,self.q)
                self.assertEqual(caught.exception.error_class,category)
                self.assertNotIn('secret-host',str(caught.exception))
                self.assertNotIn('/private/path',str(caught.exception))

    def test_nonraw_gws_requires_hosts_but_raw_uses_independent_manifest(self):
        self.q.enqueue()
        for name in ['products','raw']:
            epoch=self.q.claim(name)
            directory=self.q.root/'epochs'/epoch['verification_id']
            directory.mkdir(parents=True)
            inventory=mock.Mock()
            inventory.gws_inventory.return_value={}
            cfg=dict(self.config,gws_hosts=[])
            if name=='products':
                with self.assertRaisesRegex(RuntimeError,'host configuration') as caught:
                    recovery.collect_gws(cfg,self.q.jobs[name],epoch,directory,inventory,self.q)
                self.assertEqual(caught.exception.error_class,'config')
                inventory.gws_inventory.assert_not_called()
            else:
                self.assertEqual(recovery.collect_gws(cfg,self.q.jobs[name],epoch,directory,inventory,self.q),{})
                inventory.gws_inventory.assert_called_once()

    def test_source_root_must_be_a_readable_directory(self):
        root=Path(self.tmp.name)/'source'
        with self.assertRaises(RuntimeError) as missing:
            recovery.validate_source_root({'source':str(root)})
        self.assertEqual(missing.exception.error_class,'transient')
        root.write_text('not a directory')
        with self.assertRaises(RuntimeError) as not_directory:
            recovery.validate_source_root({'source':str(root)})
        self.assertEqual(not_directory.exception.error_class,'config')
        root.unlink()
        root.mkdir()
        with mock.patch.object(recovery.os,'access',return_value=False):
            with self.assertRaises(RuntimeError) as inaccessible:
                recovery.validate_source_root({'source':str(root)})
        self.assertEqual(inaccessible.exception.error_class,'config')
        before=recovery.validate_source_root({'source':str(root)})
        root.rename(root.with_name('old-source'))
        root.mkdir()
        with self.assertRaises(recovery.RestartEpochError):
            recovery.validate_source_root({'source':str(root)},before)

    def test_two_lanes_and_cooldown_does_not_block_other_products(self):
        self.q.enqueue()
        launched=[]
        self.q.launch_ready(launched.append)
        self.assertEqual(len(launched),2)
        self.assertIn('raw',launched)
        nonraw=next(n for n in launched if n!='raw')
        epoch=self.q.claim(nonraw)
        self.q.fail(epoch,RuntimeError('504'))
        next_launch=[]
        self.q.launch_ready(next_launch.append)
        self.assertEqual(len(next_launch),1)
        self.assertNotIn('raw',next_launch)
        self.assertNotIn(nonraw,next_launch)

    def test_repair_invalidates_collector_before_copy_and_deduplicates(self):
        self.q.enqueue(['products'])
        old=self.q.claim('products')
        with recovery.commit_lock(self.config):
            self.assertTrue(self.q.invalidate('products','repair1'))
            self.assertFalse(self.q.invalidate('products','repair1'))
        with self.assertRaises(RuntimeError):
            self.q.validate(old)
        self.assertIsNone(self.q.claim('products'))
        self.q.repair_finished('products','repair1')
        first=self.q.claim('products')
        self.assertNotEqual(first['verification_id'],old['verification_id'])
        self.q.complete(first,*self.evidence(first,stable=False))
        self.assertIsNone(self.q.claim('products'))
        self.now+=601
        second=self.q.claim('products')
        self.assertNotEqual(first['verification_id'],second['verification_id'])
        self.q.complete(second,*self.evidence(second))
        self.assertEqual(self.q.row('products')['state'],'idle')

    def test_expired_epochs_and_config_change_start_new_observations(self):
        self.q.enqueue(['raw'])
        first=self.q.claim('raw')
        self.q.fail(first,RuntimeError('504'))
        self.now=first['expires_at']+1
        second=self.q.claim('raw')
        self.assertNotEqual(first['verification_id'],second['verification_id'])
        self.q.fail(second,RuntimeError('504'))
        self.now+=100
        self.q.config['jobs'][0]['destination']='changed'
        third=self.q.claim('raw')
        self.assertNotEqual(second['verification_id'],third['verification_id'])

    def test_reboot_requeues_interrupted_observation(self):
        self.q.enqueue(['raw'])
        old=self.q.claim('raw')
        self.q.recover_workers(lambda name:False)
        new=self.q.claim('raw')
        self.assertEqual(old['verification_id'],new['verification_id'])

    def test_auth_failure_blocks_until_operator_change(self):
        class AuthError(Exception):
            error_class='auth'
        self.q.enqueue(['raw'])
        epoch=self.q.claim('raw')
        self.q.fail(epoch,AuthError('access denied'))
        self.now+=7200
        self.assertEqual(self.q.row('raw')['state'],'blocked')
        self.assertIsNone(self.q.claim('raw'))

    def test_unknown_family_rejected_and_no_false_heartbeat(self):
        with self.assertRaises(ValueError):
            self.q.enqueue(['unknown'])
        self.assertIsNone(self.q.status()['heartbeat_at'])
        self.assertEqual(self.q.status(heartbeat=True)['heartbeat_at'],recovery.iso(self.now))

    def test_raw_capacity_reserved_under_products_backlog(self):
        self.q.enqueue(['products','manifests'])
        launched=[]
        self.q.launch_ready(launched.append)
        self.assertEqual(len(launched),1)
        self.q.enqueue(['raw'])
        self.q.launch_ready(launched.append)
        self.assertEqual(launched[-1],'raw')

    def test_commit_contention_defers_without_recording_failure(self):
        self.q.enqueue(['raw'])
        epoch=self.q.claim('raw')
        self.q.defer(epoch)
        row=self.q.row('raw')
        self.assertEqual(row['state'],'retry_wait')
        self.assertEqual(row['attempt_count'],0)
        self.assertIsNone(row['first_failure_at'])
        self.assertEqual(row['verification_id'],epoch['verification_id'])

    def test_explicit_retry_clears_block_and_requires_fresh_observation(self):
        class AuthError(Exception):error_class='auth'
        self.q.enqueue(['raw'])
        old=self.q.claim('raw')
        self.q.fail(old,AuthError('denied'))
        self.q.retry('raw')
        new=self.q.claim('raw')
        self.assertNotEqual(old['verification_id'],new['verification_id'])
        self.assertGreater(new['generation'],old['generation'])

    def test_corrected_block_can_resume_same_unexpired_checkpoint(self):
        class ConfigError(Exception):error_class='config'
        self.q.enqueue(['raw'])
        old=self.q.claim('raw')
        directory=self.q.root/'epochs'/old['verification_id']
        directory.mkdir(parents=True)
        (directory/'saved-page').write_bytes(b'validated page and cursor')
        self.q.fail(old,ConfigError('publication denied'))
        self.now+=120
        self.q.retry('raw',resume_checkpoint=True)
        queued=self.q.row('raw')
        self.assertEqual(queued['state'],'queued')
        self.assertIsNone(queued['last_error'])
        resumed=self.q.claim('raw')
        for key in ('verification_id','generation','evidence_started_at','expires_at','fingerprint'):
            self.assertEqual(resumed[key],old[key])
        self.assertEqual((directory/'saved-page').read_bytes(),b'validated page and cursor')
        self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],0)

    def test_checkpoint_resume_refuses_expired_or_changed_configuration(self):
        class ConfigError(Exception):error_class='config'
        self.q.enqueue(['raw'])
        old=self.q.claim('raw')
        self.q.fail(old,ConfigError('publication denied'))
        self.now=old['expires_at']
        with self.assertRaisesRegex(ValueError,'expired'):
            self.q.retry('raw',resume_checkpoint=True)
        self.now=old['evidence_started_at']+30
        self.config['bucket']='changed'
        with self.assertRaisesRegex(ValueError,'configuration changed'):
            self.q.retry('raw',resume_checkpoint=True)
        self.assertEqual(self.q.row('raw')['state'],'blocked')
        self.assertEqual(self.q.row('raw')['verification_id'],old['verification_id'])

    def test_checkpoint_resume_never_interrupts_active_or_transient_work(self):
        with self.assertRaisesRegex(ValueError,'blocked observation'):
            self.q.retry('raw',resume_checkpoint=True)
        self.q.enqueue(['raw'])
        epoch=self.q.claim('raw')
        with self.assertRaisesRegex(ValueError,'active'):
            self.q.retry('raw',resume_checkpoint=True)
        self.q.fail(epoch,RuntimeError('504'))
        with self.assertRaisesRegex(ValueError,'blocked observation'):
            self.q.retry('raw',resume_checkpoint=True)
        self.assertEqual(self.q.row('raw')['state'],'retry_wait')

    def test_checkpoint_resume_cli_records_manual_intervention_without_new_evidence(self):
        class ConfigError(Exception):error_class='config'
        self.q.enqueue(['raw'])
        epoch=self.q.claim('raw')
        self.q.fail(epoch,ConfigError('publication denied'))
        config_path=Path(self.tmp.name)/'catalog.json'
        config_path.write_text(json.dumps(self.config))
        with mock.patch.object(sys,'argv',['recovery','--catalog',str(config_path),'retry','--job','raw','--resume-checkpoint']), mock.patch.object(recovery,'Queue',return_value=self.q), mock.patch.object(self.q,'close'), mock.patch.object(recovery,'systemd_active',return_value=False), mock.patch('builtins.print'):
            recovery.main()
        self.assertEqual(self.q.row('raw')['verification_id'],epoch['verification_id'])
        self.assertEqual(self.q.status()['manual_intervention_count'],1)
        self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],0)

    def test_completion_locks_generation_before_validation_and_repair_cannot_overtake(self):
        self.q.enqueue(['products'])
        epoch=self.q.claim('products')
        other=recovery.Queue(self.config,clock=lambda:self.now)
        self.addCleanup(other.close)
        other.db.execute('PRAGMA busy_timeout=0')
        original=self.q.validate
        attempts=[]

        def validate_locked(value):
            with self.assertRaises(sqlite3.OperationalError):
                other.invalidate('products','concurrent-repair')
            attempts.append(True)
            original(value)

        with mock.patch.object(self.q,'validate',side_effect=validate_locked):
            self.q.complete(epoch,*self.evidence(epoch))
        self.assertEqual(attempts,[True])
        self.assertTrue(other.invalidate('products','concurrent-repair'))
        self.assertEqual(self.q.row('products')['state'],'repairing')

    def test_failure_locks_generation_before_read_and_cannot_overwrite_repair(self):
        self.q.enqueue(['products'])
        epoch=self.q.claim('products')
        other=recovery.Queue(self.config,clock=lambda:self.now)
        self.addCleanup(other.close)
        other.db.execute('PRAGMA busy_timeout=0')
        original=self.q.row
        attempts=[]

        def row_locked(name):
            result=original(name)
            with self.assertRaises(sqlite3.OperationalError):
                other.invalidate('products','concurrent-repair')
            attempts.append(True)
            return result

        with mock.patch.object(self.q,'row',side_effect=row_locked):
            self.q.fail(epoch,RuntimeError('504'))
        self.assertEqual(attempts,[True])
        self.assertTrue(other.invalidate('products','concurrent-repair'))
        self.q.fail(epoch,RuntimeError('late duplicate failure'))
        self.assertEqual(self.q.row('products')['state'],'repairing')
        self.assertEqual(self.q.row('products')['generation'],epoch['generation']+1)

    def test_duplicate_completion_does_not_consume_confirmation_or_change_successor(self):
        self.q.enqueue(['raw'],confirmations=2)
        first=self.q.claim('raw')
        report,gate=self.evidence(first,stable=False)
        self.q.complete(first,report,gate)
        self.now+=601
        successor=self.q.claim('raw')
        before=self.q.row('raw')
        self.q.complete(first,report,gate)
        self.assertEqual(self.q.row('raw'),before)
        self.assertEqual(before['verification_id'],successor['verification_id'])
        self.assertEqual(before['remaining'],1)
        self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)

    def test_duplicate_failure_does_not_inflate_retry_count_or_cooldown(self):
        self.q.enqueue(['raw'])
        epoch=self.q.claim('raw')
        self.q.fail(epoch,RuntimeError('504'))
        before=self.q.row('raw')
        self.q.fail(epoch,RuntimeError('duplicate 504'))
        self.assertEqual(self.q.row('raw'),before)

    def test_mismatched_publication_identity_cannot_commit_queue_success(self):
        self.q.enqueue(['raw'])
        epoch=self.q.claim('raw')
        report,gate=self.evidence(epoch)
        report['jobs']['raw']['verification_id']='different'
        with self.assertRaises(ValueError):
            self.q.complete(epoch,report,gate)
        self.assertEqual(self.q.row('raw')['state'],'running')
        self.assertEqual(self.q.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],0)


if __name__=='__main__':
    unittest.main()
