"""Record unattended acceptance; elapsed time alone never declares success."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3
import urllib.request


def _time(value):
    return dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


def _iso(value):
    return dt.datetime.fromtimestamp(value,dt.timezone.utc).isoformat().replace('+00:00','Z')


def assess(config, report, gate, gws, public, *, now, previous, observations, batches,
           coordinator=None, resources=None):
    expected={job['name'] for job in config['jobs']}
    failures=[]
    if set(report.get('jobs',{})) != expected:
        failures.append('configured archive coverage incomplete')
    if 'raw' in expected and not gate.get('raw_retention_ready'):
        failures.append('independent raw retention gate is not ready')
    for name in sorted(expected):
        family=gate.get('families',{}).get(name,{})
        if not family.get('stable_parity'):
            failures.append(name+': two current complete confirmations required')
        deadline=8 if name=='raw' else 36
        value=report.get('jobs',{}).get(name,{})
        stamp=value.get('evidence_started_at') or value.get('verified_at')
        if not stamp or not -300<=now-_time(stamp)<deadline*3600:
            failures.append(name+': evidence expired or missing')
        for comparison in ['source_vs_s3']+([] if name=='raw' else ['source_vs_gws']):
            values=value.get(comparison,{})
            if not isinstance(values,dict) or any(values.get(field) != [] for field in ('missing_from_right','size_mismatch','checksum_mismatch')):
                failures.append(name+': settled discrepancy or missing comparison')
    if not gws.get('generated_at') or not -300<=now-_time(gws['generated_at'])<8*3600:
        failures.append('independent GWS evidence is overdue')
    streams=config.get('streams')
    if (not isinstance(streams,list) or not streams or
            any(not isinstance(s,dict) or not isinstance(s.get('name'),str) or not s['name'] for s in streams)):
        failures.append('configured GWS stream coverage is missing or invalid')
        streams=[]
    elif len({s['name'] for s in streams})!=len(streams):
        failures.append('configured GWS stream coverage is duplicated')
    for stream in streams:
        value=gws.get('streams',{}).get(stream['name'],{})
        fields=('retention_local_missing_count','retention_local_mismatch_count','retention_gws_missing_count','retention_gws_mismatch_count')
        if value.get('error') or any(type(value.get(f)) is not int or value[f] != 0 for f in fields):
            failures.append(stream['name']+': independent GWS retention counters not clean')
    if public.get('overallLevel') != 'green' or public.get('alerts') != []:
        failures.append('public operations API is not green with no alerts')
    if not public.get('updatedAt') or not -300<=now-_time(public['updatedAt'])<=900:
        failures.append('public operations API is stale')
    deployment=config.get('recovery_deployment_id','unversioned')
    state=dict(previous) if previous.get('deployment_id')==deployment else {}
    coordinator=coordinator or {}
    heartbeat=coordinator.get('heartbeat_at')
    if not heartbeat or not -300<=now-_time(heartbeat)<=900:
        failures.append('coordinator heartbeat is missing or overdue')
    if coordinator.get('state')=='blocked':
        failures.append('coordinator has a permanently blocked family')
    resource_fields=('used_bytes','max_bytes','free_bytes','reserve_bytes')
    if (not isinstance(resources,dict) or
            any(type(resources.get(key)) is not int or resources[key]<0 for key in resource_fields) or
            resources['max_bytes']<=0 or resources['used_bytes']>resources['max_bytes'] or
            resources['free_bytes']<resources['reserve_bytes']):
        failures.append('recovery storage bounds are unavailable or exceeded')
    if state.get('clean_window_started_at'):
        last=state.get('updated_at')
        if not last or not 0<=now-_time(last)<=900:
            failures.append('acceptance observation gap exceeded fifteen minutes')
        intervention=coordinator.get('last_manual_intervention_at')
        if intervention and _time(intervention)>=_time(state['clean_window_started_at']):
            failures.append('manual intervention interrupted unattended validation')
        old_count=state.get('manual_intervention_count')
        if old_count is not None and coordinator.get('manual_intervention_count',old_count)!=old_count:
            failures.append('manual intervention interrupted unattended validation')
    state['manual_intervention_count']=coordinator.get('manual_intervention_count',0)
    state['last_manual_intervention_at']=coordinator.get('last_manual_intervention_at')
    state['coordinator_heartbeat_at']=heartbeat
    state['resources']=resources
    start=_time(state['clean_window_started_at']) if state.get('clean_window_started_at') else now
    raw=[]
    seen=set()
    for observation in observations:
        identity=observation.get('verification_id')
        if not identity or identity in seen or observation.get('job')!='raw' or observation.get('clean')!=1:
            continue
        observed,completed=observation.get('started_at'),observation.get('completed_at')
        if (not isinstance(observed,(int,float)) or not isinstance(completed,(int,float)) or
                not start<=observed<=completed<=now):
            continue
        seen.add(identity)
        raw.append(observation)
    raw.sort(key=lambda row:row['started_at'])
    timeline=[start,*[row['started_at'] for row in raw],now]
    longest_raw_gap=max((right-left for left,right in zip(timeline,timeline[1:])),default=0)
    # A raw worker may occupy its full allowed four-hour observation window;
    # permit the coordinator's heartbeat grace as scheduling overhead.
    raw_gap_limit=(max(float(config.get('recovery_raw_refresh_hours',3)),
                       float(config.get('recovery_raw_epoch_hours',4)))*3600+900)
    if longest_raw_gap>raw_gap_limit:
        failures.append('successful raw observations did not continue on schedule')
    state.setdefault('deployment_id',deployment)
    state.setdefault('deployed_at',_iso(now))
    state.setdefault('sample_count',0)
    state['sample_count']+=1
    state['updated_at']=_iso(now)
    state['failures']=failures
    if failures:
        state.pop('accepted_at',None)
        state['clean_window_started_at']=None
        state['status']='under_validation'
    elif not state.get('clean_window_started_at'):
        state['clean_window_started_at']=_iso(now)
    start=_time(state['clean_window_started_at']) if state.get('clean_window_started_at') else now
    scheduled=[]
    cycle_observations=set()
    observed_by_id={row.get('verification_id'):row for row in observations if row.get('verification_id')}
    for batch in batches:
        try:
            calendar=_time(batch['id']+'T03:20:00Z')
        except (ValueError,KeyError):
            continue
        jobs=batch.get('jobs',{})
        requested=batch.get('requested_at',calendar)
        if not isinstance(jobs,dict) or set(jobs)!=expected:
            continue
        completed=batch.get('completed_at')
        if not isinstance(completed,(int,float)) or not calendar<=completed<=now:
            continue
        members=[observed_by_id.get(identity,{}) for identity in jobs.values()]
        if any(row.get('job')!=name or row.get('clean')!=1 or
               not isinstance(row.get('started_at'),(int,float)) or
               not isinstance(row.get('completed_at'),(int,float)) or
               not requested<=row['started_at']<=row['completed_at']<=completed
               for (name,_),row in zip(jobs.items(),members)):
            continue
        if set(jobs.values()) & cycle_observations:
            continue
        if calendar>=start:
            scheduled.append(batch['id'])
            cycle_observations.update(jobs.values())
    if failures:
        raw=[]
    state['clean_window_hours']=max(0,(now-start)/3600)
    state['completed_daily_audits']=sorted(scheduled)
    state['completed_raw_observations']=len(raw)
    state['longest_raw_observation_gap_hours']=longest_raw_gap/3600
    state['maximum_raw_observation_gap_hours']=raw_gap_limit/3600
    state['status']='complete' if not failures and now-start>=48*3600 and len(scheduled)>=2 and len(raw)>=12 else 'under_validation'
    if state['status']=='complete':
        state.setdefault('accepted_at',_iso(now))
    return state


def observe(config):
    from aurora_object_store_evidence import read_snapshot
    from aurora_object_store_recovery import atomic_json, recovery_root
    from aurora_object_store_s3 import check_resources
    import time
    root=recovery_root(config)
    path=root/'acceptance.json'
    previous=json.loads(path.read_text()) if path.exists() else {}
    try:
        resources=check_resources(config)
        coordinator=json.loads((root/'status.json').read_text())
        with read_snapshot(config) as snapshot:
            report,gate=snapshot.report,snapshot.gate
        gws=json.loads((Path(config['gws_manifest_root'])/'latest/summary.json').read_text())
        url=config.get('recovery_acceptance_api','https://data.gamb2le.co.uk/mobile/v1/operations')
        with urllib.request.urlopen(url,timeout=15) as response:
            public=json.load(response)
        with sqlite3.connect(root/'queue.sqlite') as db:
            db.row_factory=sqlite3.Row
            observations=[dict(r) for r in db.execute('SELECT * FROM observations')]
            batches=[dict(r) for r in db.execute('SELECT * FROM daily_audits')]
            for batch in batches:
                batch['jobs']={row['job']:row['verification_id'] for row in
                               db.execute('SELECT job,verification_id FROM daily_jobs WHERE batch=?',(batch['id'],))}
        state=assess(config,report,gate,gws,public,now=time.time(),previous=previous,observations=observations,batches=batches,coordinator=coordinator,resources=resources)
    except Exception as error:
        state={**previous,'status':'under_validation','clean_window_started_at':None,'updated_at':_iso(time.time()),'failures':['acceptance evidence unavailable: '+type(error).__name__]}
        state.pop('accepted_at',None)
    atomic_json(path,state)
    return state
