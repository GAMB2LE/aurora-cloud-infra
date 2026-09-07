import importlib.util
import copy
from contextlib import contextmanager
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import sys
import tempfile
import json

path=Path(__file__).parents[1]/'roles/object_store_mirror/files/aurora_object_store_acceptance.py'
sys.path.insert(0,str(path.parent))
spec=importlib.util.spec_from_file_location('acceptance_test',path)
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
sys.path.insert(0,str(path.parent))


class AcceptanceTests(unittest.TestCase):
    def cached_gate(self,now,*,first_raw_age=2*3600):
        from aurora_object_store_evidence import _gate_module
        cfg,_,_,gws,public=self.fixture(now)
        evaluator=_gate_module()
        previous={}
        for number in (1,2):
            jobs={}
            for name in ('raw','products'):
                age=(first_raw_age if name=='raw' else 2*3600) if number==1 else 3600
                started=a._iso(now-age)
                clean={key:[] for key in ('missing_from_right','size_mismatch','checksum_mismatch')}
                jobs[name]={'verification_id':f'{name}-{number}','verification_scope':'full_family',
                            'evidence_started_at':started,'verified_at':started,
                            'verification_completed_at':a._iso(now-age+60),
                            'source_vs_s3':clean,'source_vs_gws':clean}
            report={'generated_at':a._iso(now),'verification_mode':'full','verified_jobs':['raw','products'],'jobs':jobs}
            digest=hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest()
            previous=evaluator.evaluate(dict(cfg,_gws_summary=gws),report,previous,report_sha256=digest,
                                        now=dt.datetime.fromtimestamp(now,dt.timezone.utc))
        self.assertTrue(previous['raw_retention_ready'])
        return cfg,report,previous,gws,public,digest

    def test_corrupt_observation_record_resets_credit_without_stopping_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'recovery'
            root.mkdir()
            (root/'acceptance.json').write_text('not json')
            state=a.observe({'manifest_root':temporary,'recovery_free_reserve_bytes':0})
            self.assertEqual(state['status'],'under_validation')
            self.assertIsNone(state['clean_window_started_at'])
            self.assertEqual(json.loads((root/'acceptance.json').read_text()),state)

    def runtime(self,now):
        return {'coordinator':{'heartbeat_at':a._iso(now),'state':'idle','manual_intervention_count':0},
                'resources':{'used_bytes':1024,'max_bytes':10*1024**3,'free_bytes':100*1024**3,'reserve_bytes':50*1024**3}}

    def prior_and_observations(self,start,now):
        previous={'deployment_id':'test','acceptance_policy_version':a.ACCEPTANCE_POLICY_VERSION,
                  'clean_window_started_at':a._iso(start),'updated_at':a._iso(now-300),'manual_intervention_count':0}
        observations=[{'verification_id':f'raw-{n}','job':'raw','clean':1,'started_at':start+n*3*3600,'completed_at':start+n*3*3600+600} for n in range(16)]
        batches=[]
        for day in ('2026-09-07','2026-09-08'):
            calendar=a._time(day+'T03:20:00Z')
            members={name:f'{day}-{name}' for name in ('raw','products')}
            for name,identity in members.items():
                observations.append({'verification_id':identity,'job':name,'clean':1,'started_at':calendar+60,'completed_at':calendar+600})
            batches.append({'id':day,'requested_at':calendar,'completed_at':calendar+600,'jobs':members})
        return previous,observations,batches

    def fixture(self,now):
        comparison={k:[] for k in ['missing_from_right','size_mismatch','checksum_mismatch']}
        cfg={'jobs':[{'name':'raw'},{'name':'products'}],'streams':[{'name':'x'}],'recovery_deployment_id':'test'}
        report={'jobs':{n:{'verified_at':a._iso(now),'source_vs_s3':comparison,'source_vs_gws':comparison} for n in ['raw','products']}}
        gate={'raw_retention_ready':True,'families':{n:{'stable_parity':True} for n in ['raw','products']}}
        gws={'generated_at':a._iso(now),'streams':{'x':{f:0 for f in [
            'local_missing_count','local_mismatch_count','gws_missing_count','gws_mismatch_count',
            'retention_local_missing_count','retention_local_mismatch_count','retention_gws_missing_count','retention_gws_mismatch_count']}}}
        public={'overallLevel':'green','alerts':[],'updatedAt':a._iso(now)}
        return cfg,report,gate,gws,public

    def test_elapsed_time_and_green_alone_are_insufficient(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous={'deployment_id':'test','clean_window_started_at':a._iso(start)}
        state=a.assess(*self.fixture(now),now=now,previous=previous,observations=[],batches=[],**self.runtime(now))
        self.assertEqual(state['status'],'under_validation')

    def test_two_future_daily_cycles_and_raw_refreshes_accept(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous,obs,batches=self.prior_and_observations(start,now)
        state=a.assess(*self.fixture(now),now=now,previous=previous,observations=obs,batches=batches,**self.runtime(now))
        self.assertEqual(state['status'],'complete')
        cfg,report,gate,gws,public=self.fixture(now)
        gws['streams']['x']['retention_gws_missing_count']=1
        rejected=a.assess(cfg,report,gate,gws,public,now=now,previous=state,observations=obs,batches=batches,**self.runtime(now))
        self.assertEqual(rejected['status'],'under_validation')
        self.assertIsNone(rejected['clean_window_started_at'])
        self.assertNotIn('accepted_at',rejected)

    def test_invalid_source_metadata_rejects_clean_cached_evidence_during_retry(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous,obs,batches=self.prior_and_observations(start,now)
        for state in ('retry_wait','launching','running','queued','idle'):
            with self.subTest(state=state):
                runtime=self.runtime(now)
                runtime['coordinator'].update(state=state,jobs={'raw':{
                    'state':state,'error_class':'source_metadata',
                    'last_error':'Source modification time is invalid',
                }})
                rejected=a.assess(*self.fixture(now),now=now,previous=previous,
                                  observations=obs,batches=batches,**runtime)
                self.assertEqual(rejected['status'],'under_validation')
                self.assertIsNone(rejected['clean_window_started_at'])
                self.assertIn('raw: source metadata is invalid; fresh complete verification required',rejected['failures'])

    def test_gateway_retry_with_valid_evidence_does_not_become_source_failure(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous,obs,batches=self.prior_and_observations(start,now)
        runtime=self.runtime(now)
        runtime['coordinator'].update(state='retry_wait',jobs={'raw':{
            'state':'retry_wait','error_class':'transient','last_error':'HTTP 504',
        }})
        accepted=a.assess(*self.fixture(now),now=now,previous=previous,
                          observations=obs,batches=batches,**runtime)
        self.assertEqual(accepted['status'],'complete')

    def test_settled_stream_counters_are_required_even_when_retention_is_clean(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous,obs,batches=self.prior_and_observations(start,now)
        accepted=a.assess(*self.fixture(now),now=now,previous=previous,observations=obs,batches=batches,**self.runtime(now))
        self.assertEqual(accepted['status'],'complete')
        missing=object()
        for field in ('local_missing_count','local_mismatch_count','gws_missing_count','gws_mismatch_count'):
            for invalid in (38,-1,missing,None,False,True,'0','38',0.0,[],{}):
                with self.subTest(field=field,invalid='missing' if invalid is missing else invalid):
                    cfg,report,gate,gws,public=self.fixture(now)
                    original_gate=copy.deepcopy(gate)
                    if invalid is missing:
                        gws['streams']['x'].pop(field)
                    else:
                        gws['streams']['x'][field]=invalid
                    rejected=a.assess(cfg,report,gate,gws,public,now=now,previous=accepted,
                                      observations=obs,batches=batches,**self.runtime(now))
                    self.assertEqual(rejected['status'],'under_validation')
                    self.assertIn('x: independent settled source-to-cloud/GWS counters not clean',rejected['failures'])
                    self.assertIsNone(rejected['clean_window_started_at'])
                    self.assertEqual(rejected['clean_window_hours'],0)
                    self.assertNotIn('accepted_at',rejected)
                    self.assertEqual(gate,original_gate)
                    self.assertTrue(gate['raw_retention_ready'])
                    self.assertTrue(all(value==0 for name,value in gws['streams']['x'].items() if name.startswith('retention_')))

    def test_settled_gap_recovery_starts_a_new_clean_window(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous,obs,batches=self.prior_and_observations(start,now)
        cfg,report,gate,gws,public=self.fixture(now)
        gws['streams']['x']['local_missing_count']=38
        rejected=a.assess(cfg,report,gate,gws,public,now=now,previous=previous,
                          observations=obs,batches=batches,**self.runtime(now))
        self.assertIsNone(rejected['clean_window_started_at'])
        recovered_at=now+300
        recovered=a.assess(*self.fixture(recovered_at),now=recovered_at,previous=rejected,
                           observations=obs,batches=batches,**self.runtime(recovered_at))
        self.assertEqual(recovered['failures'],[])
        self.assertEqual(recovered['status'],'under_validation')
        self.assertEqual(recovered['clean_window_started_at'],a._iso(recovered_at))
        self.assertEqual(recovered['clean_window_hours'],0)
        self.assertEqual(recovered['completed_daily_audits'],[])

    def test_old_acceptance_policy_credit_resets_once_even_if_counters_are_clean(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        for old_version in (None,a.ACCEPTANCE_POLICY_VERSION-1):
            with self.subTest(old_version=old_version):
                previous,obs,batches=self.prior_and_observations(start,now)
                previous.update(deployed_at=a._iso(start),accepted_at=a._iso(now-300),status='complete')
                if old_version is None:
                    previous.pop('acceptance_policy_version')
                else:
                    previous['acceptance_policy_version']=old_version
                original=copy.deepcopy(previous)
                reset=a.assess(*self.fixture(now),now=now,previous=previous,
                               observations=obs,batches=batches,**self.runtime(now))
                self.assertEqual(reset['status'],'under_validation')
                self.assertEqual(reset['acceptance_policy_version'],a.ACCEPTANCE_POLICY_VERSION)
                self.assertIn('acceptance policy changed; fresh unattended window required',reset['failures'])
                self.assertIsNone(reset['clean_window_started_at'])
                self.assertNotIn('accepted_at',reset)
                self.assertEqual(reset['deployed_at'],a._iso(start))
                self.assertEqual(previous,original)
                next_sample=now+300
                fresh=a.assess(*self.fixture(next_sample),now=next_sample,previous=reset,
                               observations=obs,batches=batches,**self.runtime(next_sample))
                self.assertEqual(fresh['failures'],[])
                self.assertEqual(fresh['clean_window_started_at'],a._iso(next_sample))
                self.assertEqual(fresh['clean_window_hours'],0)
                self.assertEqual(fresh['status'],'under_validation')
                later=next_sample+300
                continuing=a.assess(*self.fixture(later),now=later,previous=fresh,
                                    observations=obs,batches=batches,**self.runtime(later))
                self.assertEqual(continuing['failures'],[])
                self.assertEqual(continuing['clean_window_started_at'],a._iso(next_sample))

    def test_fresh_unfiltered_raw_pending_does_not_reset_settled_clean_acceptance(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous,obs,batches=self.prior_and_observations(start,now)
        cfg,report,gate,gws,public=self.fixture(now)
        # Raw report source_vs_gws includes fresh edge files before the
        # independent verifier's settle cutoff. Its settled counters govern
        # acceptance, while retention retains its separate age-bounded proof.
        pending={'missing_from_right':['raw/new-in-flight-file.dat'],'size_mismatch':[],'checksum_mismatch':[]}
        report['jobs']['raw']['source_vs_gws']=copy.deepcopy(pending)
        report['jobs']['raw']['pending_upload']=copy.deepcopy(pending)
        state=a.assess(cfg,report,gate,gws,public,now=now,previous=previous,
                       observations=obs,batches=batches,**self.runtime(now))
        self.assertEqual(state['status'],'complete')
        self.assertEqual(state['failures'],[])
        self.assertEqual(state['clean_window_started_at'],a._iso(start))

    def test_unsampled_coordinator_gap_resets_window(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous,obs,batches=self.prior_and_observations(start,now)
        previous['updated_at']=a._iso(now-901)
        previous['accepted_at']=a._iso(now-300)
        state=a.assess(*self.fixture(now),now=now,previous=previous,observations=obs,batches=batches,**self.runtime(now))
        self.assertIn('acceptance observation gap exceeded fifteen minutes',state['failures'])
        self.assertIsNone(state['clean_window_started_at'])
        self.assertNotIn('accepted_at',state)

    def test_stale_heartbeat_and_storage_pressure_block_acceptance(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous,obs,batches=self.prior_and_observations(start,now)
        for failure in ('heartbeat','quota','reserve'):
            with self.subTest(failure=failure):
                runtime=self.runtime(now)
                if failure=='heartbeat':runtime['coordinator']['heartbeat_at']=a._iso(now-901)
                if failure=='quota':runtime['resources']['used_bytes']=runtime['resources']['max_bytes']+1
                if failure=='reserve':runtime['resources']['free_bytes']=runtime['resources']['reserve_bytes']-1
                state=a.assess(*self.fixture(now),now=now,previous=previous,observations=obs,batches=batches,**runtime)
                self.assertEqual(state['status'],'under_validation')
                self.assertIsNone(state['clean_window_started_at'])

    def test_raw_count_cannot_hide_a_scheduling_gap(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous,obs,batches=self.prior_and_observations(start,now)
        obs=[row for row in obs if row['verification_id'] not in ('raw-5','raw-6')]
        self.assertGreaterEqual(len(obs),12)
        state=a.assess(*self.fixture(now),now=now,previous=previous,observations=obs,batches=batches,**self.runtime(now))
        self.assertIn('successful raw observations did not continue on schedule',state['failures'])
        self.assertEqual(state['status'],'under_validation')

    def test_duplicate_incomplete_and_future_raw_rows_do_not_count(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous,obs,batches=self.prior_and_observations(start,now)
        obs=[*obs,obs[0],{'verification_id':'future','job':'raw','clean':1,'started_at':now+1,'completed_at':now+2},
             {'verification_id':'unfinished','job':'raw','clean':1,'started_at':now-1}]
        state=a.assess(*self.fixture(now),now=now,previous=previous,observations=obs,batches=batches,**self.runtime(now))
        self.assertEqual(state['completed_raw_observations'],18)

    def test_dirty_or_incomplete_daily_batch_is_not_acceptance_evidence(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        for failure in ('dirty','missing','preexisting'):
            with self.subTest(failure=failure):
                previous,obs,batches=self.prior_and_observations(start,now)
                if failure=='missing':batches[0]['jobs'].pop('products')
                else:
                    row=next(row for row in obs if row['verification_id']=='2026-09-07-products')
                    if failure=='dirty':row['clean']=0
                    if failure=='preexisting':row['started_at']=batches[0]['requested_at']-1
                state=a.assess(*self.fixture(now),now=now,previous=previous,observations=obs,batches=batches,**self.runtime(now))
                self.assertEqual(state['completed_daily_audits'],['2026-09-08'])
                self.assertEqual(state['status'],'under_validation')

    def test_backlogged_daily_ids_cannot_reuse_one_observation_cycle(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous,obs,batches=self.prior_and_observations(start,now)
        batches[0]['jobs']=dict(batches[1]['jobs'])
        batches[0]['completed_at']=batches[1]['completed_at']
        state=a.assess(*self.fixture(now),now=now,previous=previous,observations=obs,batches=batches,**self.runtime(now))
        self.assertEqual(len(state['completed_daily_audits']),1)
        self.assertEqual(state['status'],'under_validation')

    def test_manual_intervention_resets_unattended_window(self):
        start=a._time('2026-09-06T12:00:00Z');now=start+49*3600
        previous,obs,batches=self.prior_and_observations(start,now)
        runtime=self.runtime(now)
        runtime['coordinator'].update(last_manual_intervention_at=a._iso(now-300),manual_intervention_count=1)
        state=a.assess(*self.fixture(now),now=now,previous=previous,observations=obs,batches=batches,**runtime)
        self.assertIn('manual intervention interrupted unattended validation',state['failures'])
        self.assertIsNone(state['clean_window_started_at'])

    def test_gws_stream_catalogue_cannot_be_empty_or_omitted(self):
        now=a._time('2026-09-06T12:00:00Z')
        for streams in (None,[],['x'],[{'name':'x'},{'name':'x'}]):
            with self.subTest(streams=streams):
                cfg,*inputs=self.fixture(now)
                cfg['streams']=streams
                state=a.assess(cfg,*inputs,now=now,previous={},observations=[],batches=[],**self.runtime(now))
                self.assertTrue(any('configured GWS stream coverage' in f for f in state['failures']))

    def test_raw_domain_gate_and_future_evidence_cannot_be_accepted(self):
        now=a._time('2026-09-06T12:00:00Z')
        for failure in ('raw_gate','future_gws','future_raw','future_api'):
            with self.subTest(failure=failure):
                cfg,report,gate,gws,public=self.fixture(now)
                if failure=='raw_gate':gate['raw_retention_ready']=False
                if failure=='future_gws':gws['generated_at']=a._iso(now+3600)
                if failure=='future_raw':report['jobs']['raw']['verified_at']=a._iso(now+3600)
                if failure=='future_api':public['updatedAt']=a._iso(now+3600)
                state=a.assess(cfg,report,gate,gws,public,now=now,previous={},observations=[],batches=[],**self.runtime(now))
                self.assertTrue(state['failures'])

    def test_sample_reevaluates_first_proof_expiry_without_mutating_cached_gate(self):
        now=a._time('2026-09-06T12:00:00Z')
        cfg,report,cached,gws,public,digest=self.cached_gate(now,first_raw_age=8*3600-60)
        untouched=copy.deepcopy(cached)
        sampled=now+120
        fresh=a.evaluate_current_gate(cfg,report,cached,gws,report_sha256=digest,now=sampled)
        self.assertTrue(fresh['families']['raw']['clean'])
        self.assertFalse(fresh['families']['raw']['stable_parity'])
        self.assertFalse(fresh['raw_retention_ready'])
        self.assertEqual(fresh['families']['raw']['clean_streak'],1)
        self.assertEqual(cached,untouched)
        state=a.assess(cfg,report,fresh,gws,public,now=sampled,previous={},observations=[],batches=[],**self.runtime(sampled))
        self.assertIn('raw: two current complete confirmations required',state['failures'])

    def test_sample_reevaluates_independent_gws_eligibility(self):
        now=a._time('2026-09-06T12:00:00Z')
        for failure in ('expired','counter'):
            with self.subTest(failure=failure):
                cfg,report,cached,gws,_,digest=self.cached_gate(now)
                if failure=='expired':gws['generated_at']=a._iso(now-8*3600)
                else:gws['streams']['x']['retention_gws_missing_count']=1
                fresh=a.evaluate_current_gate(cfg,report,cached,gws,report_sha256=digest,now=now)
                self.assertTrue(cached['raw_retention_ready'])
                self.assertFalse(fresh['raw_retention_ready'])
                self.assertTrue(fresh['families']['raw']['clean'])

    def test_observer_samples_current_gate_before_privileged_refresh(self):
        import aurora_object_store_evidence as evidence
        import aurora_object_store_s3 as reader
        now=a._time('2026-09-06T12:00:00Z')
        cfg,report,cached,gws,public,digest=self.cached_gate(now,first_raw_age=8*3600-60)
        sampled=now+120
        @contextmanager
        def snapshot(_):
            yield SimpleNamespace(report=report,gate=cached,report_sha256=digest)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg.update(recovery_root=tmp,manifest_root=str(root/'manifests'),gws_manifest_root=str(root/'gws'))
            (root/'gws/latest').mkdir(parents=True)
            (root/'gws/latest/summary.json').write_text(json.dumps(gws))
            (root/'status.json').write_text(json.dumps(self.runtime(sampled)['coordinator']))
            with sqlite3.connect(root/'queue.sqlite') as db:
                db.executescript('CREATE TABLE observations (verification_id TEXT); CREATE TABLE daily_audits (id TEXT); CREATE TABLE daily_jobs (batch TEXT,job TEXT,verification_id TEXT);')
            with mock.patch.object(evidence,'read_snapshot',snapshot),\
                 mock.patch.object(reader,'check_resources',return_value=self.runtime(sampled)['resources']),\
                 mock.patch.object(a.urllib.request,'urlopen',return_value=io.StringIO(json.dumps(public))),\
                 mock.patch('time.time',return_value=sampled):
                state=a.observe(cfg)
            self.assertIn('raw: two current complete confirmations required',state['failures'])
            self.assertIn('independent raw retention gate is not ready',state['failures'])
            self.assertFalse((root/'manifests').exists())
            self.assertEqual(json.loads((root/'acceptance.json').read_text())['status'],'under_validation')


if __name__=='__main__':unittest.main()
