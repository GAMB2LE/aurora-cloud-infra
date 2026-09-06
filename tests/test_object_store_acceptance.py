import importlib.util
from pathlib import Path
import unittest
import sys
import tempfile
import json

path=Path(__file__).parents[1]/'roles/object_store_mirror/files/aurora_object_store_acceptance.py'
sys.path.insert(0,str(path.parent))
spec=importlib.util.spec_from_file_location('acceptance_test',path)
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)


class AcceptanceTests(unittest.TestCase):
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
        previous={'deployment_id':'test','clean_window_started_at':a._iso(start),'updated_at':a._iso(now-300),'manual_intervention_count':0}
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
        gws={'generated_at':a._iso(now),'streams':{'x':{f:0 for f in ['retention_local_missing_count','retention_local_mismatch_count','retention_gws_missing_count','retention_gws_mismatch_count']}}}
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


if __name__=='__main__':unittest.main()
