#!/usr/bin/env python3
"""Durable, independently scheduled archive family observations.

The queue is scheduling state, never verification evidence. Only the evidence
publisher can produce a certificate, after a complete frozen observation.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

CATALOG = Path('/etc/aurora-object-store/catalog.json')
ACTIVE = ('launching', 'running', 'repairing')


class RestartEpochError(RuntimeError):
    restart_epoch = True
    error_class = 'transient'


def iso(value=None):
    return dt.datetime.fromtimestamp(time.time() if value is None else value, dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def seconds(value):
    if not value:
        return 0.0
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(data, handle, separators=(',', ':'))
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {} if default is None else default


def recovery_root(config):
    return Path(config.get('recovery_root', str(Path(config['manifest_root']) / 'recovery')))


def boot_id():
    path = Path('/proc/sys/kernel/random/boot_id')
    return path.read_text().strip() if path.exists() else 'local'


def fingerprint(config, job):
    values = {key: config.get(key) for key in ('remote', 'bucket', 's3_region', 's3_addressing_style', 'gws_hosts', 'gws_manifest_root', 'gws_user', 'gws_settle_seconds', 'streams')}
    credentials=os.environ.get('CREDENTIALS_DIRECTORY')
    path=Path(credentials)/'s3-rclone-config' if credentials else Path(config['rclone_config']) if config.get('rclone_config') else None
    if path and path.is_file():
        # Bind a resumed listing to the exact endpoint/credential configuration
        # without persisting credential material in the queue or checkpoint.
        values['s3_configuration_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    values.update(job=job, algorithm=1)
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def validate_epoch(config, epoch):
    """Read-only generation check usable from S3 page worker threads."""
    with sqlite3.connect(recovery_root(config)/'queue.sqlite',timeout=30) as db:
        row=db.execute('SELECT state,generation,verification_id FROM jobs WHERE name=?',(epoch['name'],)).fetchone()
    if not row or row != ('running',epoch['generation'],epoch['verification_id']):
        raise RuntimeError('archive observation invalidated by repair or successor')
    if time.time() >= epoch['expires_at']:
        raise RuntimeError('archive observation window expired')


@contextmanager
def commit_lock(config):
    path = Path(config['manifest_root']) / '.inventory.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


class Queue:
    def __init__(self, config, clock=time.time):
        self.config, self.clock = config, clock
        self.root = recovery_root(config)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'queue.sqlite'
        self.jobs = {job['name']: job for job in config['jobs']}
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS jobs (
            name TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'idle',
            generation INTEGER NOT NULL DEFAULT 0, verification_id TEXT,
            evidence_started_at REAL, expires_at REAL, fingerprint TEXT,
            queued_at REAL, next_retry_at REAL NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0, first_failure_at REAL,
            last_error TEXT, error_class TEXT, owner_pid INTEGER, owner_boot TEXT,
            heartbeat_at REAL, remaining INTEGER NOT NULL DEFAULT 0,
            last_success_at REAL, last_evidence_at REAL,
            progress TEXT NOT NULL DEFAULT '{}');
          CREATE TABLE IF NOT EXISTS daily_audits (
            id TEXT PRIMARY KEY, requested_at REAL NOT NULL, completed_at REAL);
          CREATE TABLE IF NOT EXISTS daily_jobs (
            batch TEXT NOT NULL, job TEXT NOT NULL, verification_id TEXT,
            PRIMARY KEY(batch,job));
          CREATE TABLE IF NOT EXISTS observations (
            verification_id TEXT PRIMARY KEY, job TEXT NOT NULL,
            started_at REAL NOT NULL, completed_at REAL NOT NULL,
            clean INTEGER NOT NULL, report_sha256 TEXT);
          CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS repair_requests (
            repair_id TEXT NOT NULL, job TEXT NOT NULL, generation INTEGER NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(repair_id,job));
        ''')
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO jobs(name) VALUES (?)', [(n,) for n in self.jobs])
        if os.geteuid() == 0:
            st = self.root.stat()
            os.chown(self.path, st.st_uid, st.st_gid)
        os.chmod(self.path, 0o660)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        """Serialize state decisions with their writes across worker processes."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def row(self, name):
        if name not in self.jobs:
            raise ValueError('unknown catalogue family: ' + name)
        return dict(self.db.execute('SELECT * FROM jobs WHERE name=?', (name,)).fetchone())

    def rows(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM jobs ORDER BY name') if r['name'] in self.jobs]

    def enqueue(self, names=None, *, daily=False, confirmations=1, batch_id=None):
        names = list(self.jobs if names is None else names)
        for name in names:
            self.row(name)
        now = self.clock()
        with self.transaction():
            if daily:
                batch_id = batch_id or dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%Y-%m-%d')
                self.db.execute('INSERT OR IGNORE INTO daily_audits(id,requested_at) VALUES (?,?)', (batch_id, now))
                self.db.executemany('INSERT OR IGNORE INTO daily_jobs(batch,job) VALUES (?,?)', [(batch_id, n) for n in self.jobs])
                names = list(self.jobs)
            for name in names:
                row = self.row(name)
                # A request never revokes an in-flight observation or cooldown.
                state = 'queued' if row['state'] == 'idle' else row['state']
                self.db.execute('UPDATE jobs SET state=?, remaining=MAX(remaining,?), queued_at=COALESCE(queued_at,?) WHERE name=?', (state, confirmations, now, name))
        return batch_id

    def seed_and_schedule(self, report, gate):
        now = self.clock()
        with self.transaction():
            for name in self.jobs:
                row = self.row(name)
                value = report.get('jobs', {}).get(name, {})
                observed = seconds(value.get('evidence_started_at') or value.get('verified_at'))
                if row['last_evidence_at'] is None and observed:
                    self.db.execute('UPDATE jobs SET last_evidence_at=? WHERE name=?', (observed, name))
                hours = self.config.get('recovery_raw_refresh_hours', 3) if name == 'raw' else self.config.get('recovery_products_refresh_hours', 12)
                family = gate.get('families', {}).get(name, {})
                needs_confirmation = not family.get('stable_parity', False)
                if row['state'] == 'idle' and (not observed or now-observed >= float(hours)*3600 or needs_confirmation):
                    self.db.execute("UPDATE jobs SET state='queued',remaining=MAX(remaining,1),queued_at=COALESCE(queued_at,?) WHERE name=?", (now, name))
            date = dt.datetime.fromtimestamp(now, dt.timezone.utc)
            due = date.replace(hour=3, minute=20, second=0, microsecond=0)
        if date >= due:
            key = date.strftime('%Y-%m-%d')
            if not self.db.execute('SELECT 1 FROM daily_audits WHERE id=?', (key,)).fetchone():
                self.enqueue(daily=True, batch_id=key)

    def claim(self, name):
        now = self.clock()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.row(name)
            if row['state'] not in ('queued','retry_wait','launching') or row['next_retry_at'] > now:
                self.db.rollback()
                return None
            fp = fingerprint(self.config, self.jobs[name])
            if not row['verification_id'] or (row['expires_at'] or 0) <= now or row['fingerprint'] != fp:
                hours = self.config.get('recovery_raw_epoch_hours',4) if name == 'raw' else self.config.get('recovery_products_epoch_hours',12)
                row.update(verification_id=str(uuid.uuid4()), evidence_started_at=now, expires_at=now+float(hours)*3600, fingerprint=fp)
            self.db.execute("UPDATE jobs SET state='running',verification_id=?,evidence_started_at=?,expires_at=?,fingerprint=?,owner_pid=?,owner_boot=?,heartbeat_at=? WHERE name=?", (row['verification_id'],row['evidence_started_at'],row['expires_at'],row['fingerprint'],os.getpid(),boot_id(),now,name))
            self.db.commit()
            return self.row(name)
        except BaseException:
            self.db.rollback()
            raise

    def validate(self, epoch):
        current = self.row(epoch['name'])
        if current['state'] != 'running' or current['generation'] != epoch['generation'] or current['verification_id'] != epoch['verification_id']:
            raise RuntimeError('archive observation invalidated by repair or successor')
        if self.clock() >= epoch['expires_at']:
            raise RuntimeError('archive observation window expired')

    def heartbeat(self, epoch, progress=None):
        with self.transaction():
            self.db.execute('UPDATE jobs SET heartbeat_at=?,progress=? WHERE name=? AND verification_id=? AND generation=? AND state=\'running\'', (self.clock(),json.dumps(progress or {}),epoch['name'],epoch['verification_id'],epoch['generation']))

    def fail(self, epoch, error, *, restart_epoch=False):
        with self.transaction():
            row = self.row(epoch['name'])
            if (row['state'] != 'running' or row['verification_id'] != epoch['verification_id'] or
                    row['generation'] != epoch['generation']):
                return
            category = getattr(error, 'error_class', 'transient')
            count = row['attempt_count']+1
            delays = self.config.get('recovery_retry_delays_seconds', [900,1800,3600])
            delay = min(3600,float(delays[min(count-1,len(delays)-1)]))
            delay = min(3600,delay+random.uniform(0,min(60,delay/10)))
            state = 'blocked' if category in ('auth','config') else 'retry_wait'
            self.db.execute('UPDATE jobs SET state=?,attempt_count=?,first_failure_at=COALESCE(first_failure_at,?),next_retry_at=?,last_error=?,error_class=?,owner_pid=NULL,verification_id=? WHERE name=? AND generation=? AND verification_id=? AND state=\'running\'', (state,count,self.clock(),self.clock()+delay,str(error)[:1000],category,None if restart_epoch else epoch['verification_id'],epoch['name'],epoch['generation'],epoch['verification_id']))

    def defer(self, epoch, delay=30):
        """Busy commit lock is scheduling pressure, not a failed observation."""
        with self.transaction():
            self.db.execute("UPDATE jobs SET state='retry_wait',next_retry_at=?,owner_pid=NULL WHERE name=? AND generation=? AND verification_id=? AND state='running'",(self.clock()+delay,epoch['name'],epoch['generation'],epoch['verification_id']))

    def retry(self,name):
        with self.transaction():
            row=self.row(name)
            if row['state'] in ACTIVE:
                raise ValueError('family is active; retry request cannot interrupt it')
            self.db.execute("UPDATE jobs SET state='queued',generation=generation+1,verification_id=NULL,next_retry_at=0,remaining=MAX(remaining,1),attempt_count=0,first_failure_at=NULL,error_class=NULL,last_error=NULL,queued_at=? WHERE name=?",(self.clock(),name))

    def complete(self, epoch, report, gate):
        now = self.clock()
        family = gate.get('families', {}).get(epoch['name'], {})
        values = report['jobs'][epoch['name']]
        clean = bool(family.get('clean', False))
        with self.transaction():
            # Retrying a queue commit after a crash must not consume a second
            # confirmation or overwrite a successor/repair state.
            if self.db.execute('SELECT 1 FROM observations WHERE verification_id=?', (epoch['verification_id'],)).fetchone():
                return
            self.validate(epoch)
            if values.get('verification_id') != epoch['verification_id']:
                raise ValueError('published family does not match the completing observation')
            self.db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?)', (epoch['verification_id'],epoch['name'],epoch['evidence_started_at'],now,int(clean),gate.get('report_sha256')))
            remaining = max(0,self.row(epoch['name'])['remaining']-int(clean))
            if clean and not family.get('stable_parity',False):
                remaining = max(remaining,1)
            # Each daily audit needs its own observation. A post-outage scan
            # cannot complete multiple outstanding daily cycles at once.
            self.db.execute('UPDATE daily_jobs SET verification_id=? WHERE job=? AND verification_id IS NULL AND batch=(SELECT d.batch FROM daily_jobs d JOIN daily_audits a ON a.id=d.batch WHERE d.job=? AND d.verification_id IS NULL AND a.requested_at<=? ORDER BY a.requested_at,a.id LIMIT 1)', (epoch['verification_id'],epoch['name'],epoch['name'],epoch['evidence_started_at']))
            self.db.execute('UPDATE daily_audits SET completed_at=? WHERE completed_at IS NULL AND NOT EXISTS (SELECT 1 FROM daily_jobs WHERE daily_jobs.batch=daily_audits.id AND verification_id IS NULL)', (now,))
            newer_request = bool(self.db.execute('SELECT 1 FROM daily_jobs WHERE job=? AND verification_id IS NULL', (epoch['name'],)).fetchone())
            # A complete discrepancy is evidence; retire its checkpoints. Copy
            # repair receives the report and starts new confirmation epochs.
            state = 'queued' if remaining or newer_request else 'idle'
            self.db.execute('UPDATE jobs SET state=?,verification_id=NULL,evidence_started_at=NULL,expires_at=NULL,remaining=?,last_success_at=?,last_evidence_at=?,attempt_count=0,first_failure_at=NULL,last_error=NULL,error_class=NULL,owner_pid=NULL,next_retry_at=?,queued_at=? WHERE name=? AND generation=? AND verification_id=? AND state=\'running\'', (state,remaining,now,seconds(values.get('evidence_started_at') or values['verified_at']),now+(600 if remaining else 0),now if state=='queued' else None,epoch['name'],epoch['generation'],epoch['verification_id']))

    def invalidate(self, name, repair_id):
        """Caller must hold the canonical commit lock before starting copies."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            prior=self.db.execute('SELECT * FROM repair_requests WHERE repair_id=? AND job=?',(repair_id,name)).fetchone()
            if prior:
                self.db.rollback()
                return False
            row=self.row(name)
            generation=row['generation']+1
            self.db.execute('INSERT INTO repair_requests VALUES (?,?,?,0)',(repair_id,name,generation))
            self.db.execute("UPDATE jobs SET generation=?,state='repairing',verification_id=NULL,remaining=2,queued_at=?,owner_pid=NULL WHERE name=?",(generation,self.clock(),name))
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def repair_finished(self, name, repair_id, success=True):
        with self.transaction():
            record=self.db.execute('SELECT * FROM repair_requests WHERE repair_id=? AND job=?',(repair_id,name)).fetchone()
            if not record or record['completed']:
                return
            self.db.execute('UPDATE repair_requests SET completed=1 WHERE repair_id=? AND job=?',(repair_id,name))
            # Failed copy also needs a new observation; successful subsets must
            # never leave the family permanently stuck in repairing.
            self.db.execute("UPDATE jobs SET state='queued',remaining=2,next_retry_at=?,last_error=?,error_class=? WHERE name=? AND generation=?",(self.clock(),None if success else 'exact-path repair failed; fresh comparison queued',None if success else 'transient',name,record['generation']))

    def recover_workers(self, active):
        """Reconcile durable intent against live units after crash or reboot."""
        now=self.clock()
        with self.transaction():
            for row in self.rows():
                if row['state'] not in ('running','launching'):
                    continue
                if active(row['name']):
                    continue
                if row['state']=='launching' and now-(row['heartbeat_at'] or 0)<120:
                    continue
                self.db.execute("UPDATE jobs SET state='retry_wait',next_retry_at=?,owner_pid=NULL,last_error='worker interrupted; checkpoint preserved',error_class='transient',first_failure_at=COALESCE(first_failure_at,?) WHERE name=?",(now,now,row['name']))
            repairing=bool(self.db.execute("SELECT 1 FROM jobs WHERE state='repairing'").fetchone())
        return repairing

    def launch_ready(self, launch):
        launched=[]
        now=self.clock()
        for lane in ('raw','products'):
            self.db.execute('BEGIN IMMEDIATE')
            rows=self.rows()
            in_lane=lambda r: (r['name']=='raw') == (lane=='raw')
            if any(in_lane(r) and r['state'] in ('running','launching') for r in rows):
                self.db.rollback()
                continue
            candidates=[r for r in rows if in_lane(r) and r['state'] in ('queued','retry_wait') and r['next_retry_at']<=now]
            if not candidates:
                self.db.rollback()
                continue
            chosen=min(candidates,key=lambda r:(r['last_evidence_at'] or 0,r['queued_at'] or 0,r['name']))
            self.db.execute("UPDATE jobs SET state='launching',heartbeat_at=? WHERE name=?",(now,chosen['name']))
            self.db.commit()
            try:
                launch(chosen['name'])
            except Exception as error:
                with self.transaction():
                    self.db.execute("UPDATE jobs SET state='retry_wait',next_retry_at=?,last_error=?,error_class='transient',first_failure_at=COALESCE(first_failure_at,?) WHERE name=? AND state='launching' AND generation=?",(now+300,str(error)[:1000],now,chosen['name'],chosen['generation']))
            else:
                launched.append(chosen['name'])
        return launched

    def status(self, heartbeat=False):
        now=self.clock()
        if heartbeat:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('heartbeat_at',?)",(iso(now),))
        stamp=self.db.execute("SELECT value FROM metadata WHERE key='heartbeat_at'").fetchone()
        jobs={}
        for row in self.rows():
            for field in ('queued_at','next_retry_at','first_failure_at','last_success_at','last_evidence_at','evidence_started_at','expires_at','heartbeat_at'):
                row[field]=iso(row[field]) if row[field] else None
            row['progress']=json.loads(row['progress'])
            jobs[row.pop('name')]=row
        states={j['state'] for j in jobs.values()}
        state=next((s for s in ('blocked','running','launching','repairing','retry_wait','queued') if s in states),'idle')
        if state in ('launching','repairing'):
            state='running'
        batches={r['id']:{'requested_at':iso(r['requested_at']),'completed_at':iso(r['completed_at']) if r['completed_at'] else None,'jobs':{j['job']:j['verification_id'] for j in self.db.execute('SELECT * FROM daily_jobs WHERE batch=?',(r['id'],))}} for r in self.db.execute('SELECT * FROM daily_audits ORDER BY requested_at DESC LIMIT 5')}
        last_full=self.db.execute('SELECT MAX(completed_at) FROM daily_audits').fetchone()[0]
        def extreme(field, fn):
            values=[j[field] for j in jobs.values() if j[field]]
            return fn(values) if values else None
        retry_times=[j['next_retry_at'] for j in jobs.values() if j['state']=='retry_wait' and j['next_retry_at']]
        metadata=dict(self.db.execute('SELECT key,value FROM metadata'))
        return dict(schema_version=1,state=state,updated_at=iso(now),heartbeat_at=stamp[0] if stamp else None,jobs=jobs,failed_jobs=[n for n,j in jobs.items() if j['error_class']],last_success_at=extreme('last_success_at',max),oldest_pending_at=extreme('queued_at',min),next_retry_at=min(retry_times) if retry_times else None,daily_audits=batches,last_full_audit_at=iso(last_full) if last_full else None,last_manual_intervention_at=metadata.get('last_manual_intervention_at'),manual_intervention_count=int(metadata.get('manual_intervention_count','0')))

    def record_manual_intervention(self):
        with self.transaction():
            row=self.db.execute("SELECT value FROM metadata WHERE key='manual_intervention_count'").fetchone()
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('manual_intervention_count',?)",(str(int(row[0]) + 1 if row else 1),))
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('last_manual_intervention_at',?)",(iso(self.clock()),))


def load_inventory():
    path=Path(__file__).with_name('aurora_object_store_inventory.py')
    if not path.exists():
        path=Path(__file__).with_name('aurora-object-store-inventory.py')
    spec=importlib.util.spec_from_file_location('aurora_inventory_legacy',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect_gws(config,job,epoch,directory,inventory,queue):
    """Keep a completed non-raw GWS scan across S3 retries in the same epoch."""
    from aurora_object_store_s3 import resource_lock, InventoryError
    path=directory/'gws.json'
    identity=dict(verification_id=epoch['verification_id'],generation=epoch['generation'],fingerprint=epoch['fingerprint'],source_sha256=load_json(directory/'epoch.json').get('source_sha256'))
    if job['name']!='raw' and path.exists():
        try:
            payload=path.read_bytes()
            metadata=load_json(directory/'gws-checkpoint.json')
            if metadata.get('identity')!=identity or metadata.get('payload_sha256')!=hashlib.sha256(payload).hexdigest():
                raise ValueError('GWS checkpoint identity or digest mismatch')
            if not epoch['evidence_started_at']<=metadata['started_at']<=metadata['completed_at']<=epoch['expires_at']:
                raise ValueError('GWS checkpoint observation window mismatch')
            rows=json.loads(payload)
            if not isinstance(rows,dict):
                raise ValueError('GWS checkpoint must contain a complete inventory')
        except (ValueError,KeyError,TypeError) as error:
            raise RestartEpochError('completed GWS checkpoint invalid') from error
        queue.validate(epoch)
        return rows
    started=queue.clock()
    attempt_config=dict(config,gws_inventory_attempts=1,gws_inventory_retry_delay_seconds=0)
    if job['name']!='raw':
        hosts=config.get('gws_hosts')
        if (not isinstance(hosts,list) or not hosts or
                any(not isinstance(host,str) or not host.strip() for host in hosts)):
            raise InventoryError('GWS transfer host configuration is missing or invalid','config')
        attempt_config['gws_hosts']=[hosts[int(epoch.get('attempt_count',0))%len(hosts)]]
    try:
        # One remote invocation per queue attempt. A failed host releases this
        # worker; the coordinator owns delay, failover, and outage escalation.
        rows=inventory.gws_inventory(attempt_config,job)
    except (KeyError,TypeError):
        raise InventoryError('GWS inventory configuration is missing or invalid','config') from None
    except (OSError,RuntimeError,ValueError,subprocess.SubprocessError) as error:
        detail=(str(error)+' '+str(getattr(error,'stderr','') or '')).lower()
        if 'permission denied' in detail and 'publickey' in detail:
            raise InventoryError('GWS SSH authentication failed','auth') from None
        config_errors=('host key verification failed','remote host identification has changed',
                       'offending ','bad configuration option','no such identity',
                       'invalid format','bad permissions','unprotected private key file',
                       'known_hosts: permission denied')
        if any(pattern in detail for pattern in config_errors):
            raise InventoryError('GWS SSH host-key or connection configuration is invalid','config') from None
        raise InventoryError('GWS inventory unavailable; coordinator will retry another host','transient') from None
    queue.validate(epoch)
    if job['name']!='raw':
        with resource_lock(config,extra_bytes=len(json.dumps(rows).encode())+4096):
            atomic_json(path,rows)
            atomic_json(directory/'gws-checkpoint.json',dict(identity=identity,payload_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),started_at=started,completed_at=queue.clock()))
        queue.validate(epoch)
    return rows


def validate_source_root(job, expected=None):
    """Never interpret an absent or unreadable configured source as empty."""
    from aurora_object_store_s3 import InventoryError
    source=job.get('source')
    if not isinstance(source,str) or not source:
        raise InventoryError('Archive source root configuration is invalid','config')
    root=Path(source)
    try:
        info=root.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise InventoryError('Archive source root is not a directory','config')
        if not os.access(root,os.R_OK|os.X_OK):
            raise InventoryError('Archive source root is not accessible','config')
        # access() is advisory; opening the directory verifies the actual
        # worker's read access and catches a removal between stat and scan.
        with os.scandir(root):
            pass
    except FileNotFoundError:
        raise InventoryError('Archive source root is unavailable; observation deferred','transient') from None
    except PermissionError:
        raise InventoryError('Archive source root is not accessible','config') from None
    except OSError:
        raise InventoryError('Archive source root could not be inspected; observation deferred','transient') from None
    identity=(info.st_dev,info.st_ino)
    if expected is not None and identity!=expected:
        raise RestartEpochError('Archive source root changed during observation')
    return identity


def run_worker(config,name,*,shadow=False):
    from aurora_object_store_s3 import PagedS3Lister, check_resources, resource_lock, InventoryError
    from aurora_object_store_evidence import publish_family
    queue=Queue(config)
    lane='raw' if name=='raw' else 'products'
    with (queue.root/('worker-'+lane+'.lock')).open('a+') as lane_lock:
        try:
            fcntl.flock(lane_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            queue.close()
            return 0
        previous=queue.row(name)
        epoch=queue.claim(name)
        if not epoch:
            queue.close()
            return 0
        directory=queue.root/'epochs'/epoch['verification_id']
        directory.mkdir(parents=True,exist_ok=True)
        if previous['verification_id'] and previous['verification_id'] != epoch['verification_id']:
            retire_epoch(queue.root,previous['verification_id'])
        stopped=threading.Event()
        phase={'phase':'starting'}
        lister=None
        def heartbeat():
            q=Queue(config)
            try:
                while not stopped.wait(30):
                    q.heartbeat(epoch,dict(phase))
            finally:
                q.close()
        thread=threading.Thread(target=heartbeat,daemon=True)
        thread.start()
        try:
            inv=load_inventory()
            job=queue.jobs[name]
            frozen=directory/'source.json'
            phase['phase']='local_inventory'
            source_identity=validate_source_root(job)
            if frozen.exists():
                try:
                    metadata=load_json(directory/'epoch.json')
                    raw=frozen.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != metadata.get('source_sha256'):
                        raise ValueError('frozen source snapshot digest mismatch')
                    if metadata.get('source_root_identity')!=list(source_identity):
                        raise ValueError('frozen source root changed or is not bound')
                    snapshot=json.loads(raw)
                except (ValueError,KeyError,TypeError) as error:
                    raise RestartEpochError('frozen source checkpoint invalid') from error
            else:
                check_resources(config)
                patterns=inv.COMMON_EXCLUDES+job.get('exclude',[])
                live=inv.local_inventory(job['source'],patterns,'0s',bool(job.get('copy_links')),strict_errors=True)
                settled=inv.local_inventory(job['source'],patterns,inv.verification_settle_age(job),bool(job.get('copy_links')),strict_errors=True)
                validate_source_root(job,source_identity)
                snapshot={'local':settled,'pending':{k:v for k,v in live.items() if k not in settled}}
                # The manifest can be large; account for its encoded bytes too.
                with resource_lock(config,extra_bytes=len(json.dumps(snapshot).encode())+4096):
                    atomic_json(frozen,snapshot)
                    atomic_json(directory/'epoch.json',dict(job=name,verification_id=epoch['verification_id'],evidence_started_at=iso(epoch['evidence_started_at']),source_root_identity=list(source_identity),source_sha256=hashlib.sha256(frozen.read_bytes()).hexdigest()))
            phase['phase']='gws_inventory'
            # Completed direct product scans survive S3 retries. Raw still
            # reads current independent canonical GWS stream evidence.
            phase['gws_checkpoint_reused']=name!='raw' and (directory/'gws.json').exists()
            gws=collect_gws(config,job,epoch,directory,inv,queue)
            queue.validate(epoch)
            phase['phase']='object_store_inventory'
            collection_config=dict(config,_recovery_deadline=epoch['expires_at'],_validate_epoch=lambda:validate_epoch(config,epoch),_progress_callback=lambda progress:phase.update(progress))
            lister=PagedS3Lister(collection_config,directory,epoch['verification_id'])
            s3=lister.inventory(job,snapshot['local'])
            queue.validate(epoch)
            phase['phase']='stability_check'
            validate_source_root(job,source_identity)
            local=inv.retain_unchanged_local_snapshot(job['source'],snapshot['local'],bool(job.get('copy_links')))
            validate_source_root(job,source_identity)
            gws_source=inv.mirror_manifest_inventory(config,job,'source') if name=='raw' else local
            artifacts=directory/'artifacts'
            artifacts.mkdir(exist_ok=True)
            inventories=[('local',local),('s3',s3),('gws',gws),('gws-source',gws_source)]
            # TSV quoting can double field bytes. Reserve all output files,
            # including a partial previous attempt, before opening any writer.
            artifact_bytes=4096+sum(16+sum(2*len(str(row.get(field,'')).encode()) for field in ('relative_path','size','mtime','checksum')) for _,rows in inventories for row in rows.values())
            with resource_lock(config,extra_bytes=artifact_bytes):
                for suffix,rows in inventories:
                    inv.write_tsv(artifacts/f'{name}-{suffix}.tsv',rows)
            observed=iso(epoch['evidence_started_at'])
            values=dict(source=job['source'],gws=job.get('gws_destination',''),s3=f"s3://{config['bucket']}/{job['destination'].strip('/')}",source_vs_s3=inv.compare(local,s3),pending_upload=inv.compare(snapshot['pending'],s3),verification_settle_age=inv.verification_settle_age(job),source_vs_gws=inv.compare(gws_source,gws),gws_vs_s3=None,gws_evidence='independent canonical stream manifests' if name=='raw' else 'direct remote GWS inventory',verification_id=epoch['verification_id'],evidence_started_at=observed,verified_at=observed,verification_completed_at=iso(),verification_scope='full_family')
            phase['phase']='publishing'
            queue.heartbeat(epoch,dict(phase))
            if shadow:
                atomic_json(directory/'shadow-comparison.json',values)
                with queue.db:
                    queue.db.execute("UPDATE jobs SET state='idle',remaining=0,owner_pid=NULL WHERE name=?",(name,))
            else:
                report,gate=publish_family(config,name,values,artifacts,validate_epoch=lambda:queue.validate(epoch))
                queue.complete(epoch,report,gate)
                # Privileged unit post-hooks evaluate the gate, request exact
                # repair and safely trigger retention after this worker exits.
            print(json.dumps({'job':name,'verification_id':epoch['verification_id'],'state':'complete','shadow':shadow}))
            return 0
        except BlockingIOError:
            queue.heartbeat(epoch,dict(phase))
            queue.defer(epoch)
            return 0
        except Exception as error:
            if isinstance(error,PermissionError):
                error=InventoryError('Archive source or recovery storage is not accessible','config')
            # Short attempts can fail before the 30-second heartbeat. Preserve
            # their last committed page progress before changing queue state.
            try:
                queue.heartbeat(epoch,dict(phase))
            except (sqlite3.Error,OSError):
                pass
            queue.fail(epoch,error,restart_epoch=bool(getattr(error,'restart_epoch',False)))
            print(json.dumps({'job':name,'state':'deferred','error_class':getattr(error,'error_class','transient'),'error':str(error)[:1000]}),file=sys.stderr)
            return 0  # durable queue owns retries; unit exits do not certify data
        finally:
            stopped.set()
            thread.join(timeout=2)
            if lister is not None and hasattr(lister,'close'):
                lister.close()
            if not shadow and queue.row(name)['verification_id'] != epoch['verification_id']:
                retire_epoch(queue.root,epoch['verification_id'])
            queue.close()


def retire_epoch(root, identifier):
    """Remove only a known, unreferenced worker checkpoint UUID."""
    if str(uuid.UUID(identifier)) != identifier:
        raise ValueError('invalid checkpoint identity')
    directory=root/'epochs'/identifier
    if directory.is_symlink():
        raise ValueError('checkpoint directory cannot be a symlink')
    if directory.is_dir():
        shutil.rmtree(directory)


def cleanup_orphan_epochs(queue):
    """Reclaim only expired UUID checkpoints no queue can resume.

    The 15-hour grace exceeds the worker unit's hard 13-hour lifetime. An
    invalidated worker cannot still be writing when its orphan is reclaimed.
    Referenced epochs are retained even while blocked through a long outage.
    """
    parent=queue.root/'epochs'
    if not parent.is_dir():
        return []
    referenced={r['verification_id'] for r in queue.rows() if r['verification_id']}
    removed=[]
    for path in parent.iterdir():
        try:
            valid=str(uuid.UUID(path.name))==path.name
        except ValueError:
            continue
        if valid and path.name not in referenced and not path.is_symlink() and path.is_dir() and queue.clock()-path.stat().st_mtime>15*3600:
            retire_epoch(queue.root,path.name)
            removed.append(path.name)
    return removed


def systemd_active(name):
    result=subprocess.run(['/bin/systemctl','show',f'aurora-object-store-recovery-worker@{name}.service','--property=ActiveState','--value'],capture_output=True,text=True,check=False)
    return result.stdout.strip() in ('active','activating','deactivating')


def tick(config,*,launch=True):
    from aurora_object_store_evidence import read_snapshot, cleanup_generations
    queue=Queue(config)
    try:
        try:
            with read_snapshot(config) as snapshot:
                report,gate=snapshot.report,snapshot.gate
        except BlockingIOError:
            atomic_json(queue.root/'status.json',queue.status(heartbeat=True))
            return
        except FileNotFoundError:
            report,gate={},{}
        queue.seed_and_schedule(report,gate)
        repairing=queue.recover_workers(systemd_active)
        if repairing and launch:
            subprocess.run(['/bin/systemctl','--no-block','start','aurora-object-store-repair.service'],check=False)
        if launch:
            queue.launch_ready(lambda n:subprocess.run(['/bin/systemctl','--no-block','start',f'aurora-object-store-recovery-worker@{n}.service'],check=True,capture_output=True,text=True))
        atomic_json(queue.root/'status.json',queue.status(heartbeat=True))
        cleanup_orphan_epochs(queue)
        try:
            cleanup_generations(config)
        except BlockingIOError:
            pass  # a concurrent publication owns the short commit lock
        if launch and config.get('recovery_enabled'):
            from aurora_object_store_acceptance import observe
            observe(config)
    finally:
        queue.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',type=Path,default=CATALOG)
    commands=parser.add_subparsers(dest='command',required=True)
    enqueue=commands.add_parser('enqueue')
    enqueue.add_argument('--job',action='append')
    enqueue.add_argument('--daily',action='store_true')
    enqueue.add_argument('--confirmations',type=int,default=1)
    task=commands.add_parser('tick');task.add_argument('--no-launch',action='store_true')
    commands.add_parser('status')
    commands.add_parser('upload')
    for name in ('worker','shadow','invalidate','retry'):
        command=commands.add_parser(name);command.add_argument('--job',required=True)
    args=parser.parse_args()
    config=load_json(args.catalog)
    config['_catalog_path']=str(args.catalog)
    if args.command=='tick':
        tick(config,launch=not args.no_launch)
    elif args.command=='upload':
        from aurora_object_store_evidence import read_snapshot
        credentials=os.environ.get('CREDENTIALS_DIRECTORY')
        credential=str(Path(credentials)/'s3-rclone-config') if credentials else config['rclone_config']
        remote=f"{config['remote']}:{config['bucket']}/data/internal/aurora-cloud/manifests/object-store/latest"
        with read_snapshot(config) as snapshot:
            subprocess.run(['/usr/bin/rclone','copy',str(snapshot.path),remote,'--config='+credential,'--exclude=.pin.lock','--exclude=comparison.json','--contimeout=30s','--timeout=10m','--retries=2'],check=True)
            subprocess.run(['/usr/bin/rclone','copyto',str(snapshot.path/'comparison.json'),remote+'/comparison.json','--config='+credential,'--contimeout=30s','--timeout=10m','--retries=2'],check=True)
    elif args.command in ('worker','shadow'):
        if args.command=='shadow':
            # A shadow catalog must use an isolated manifest root; reject any
            # production-enabled catalogue to protect canonical evidence.
            if config.get('recovery_enabled') or not config.get('recovery_shadow'):
                parser.error('shadow requires recovery_shadow=true and recovery_enabled=false')
            q=Queue(config);q.enqueue([args.job]);q.close()
        return run_worker(config,args.job,shadow=args.command=='shadow')
    else:
        queue=Queue(config)
        try:
            if args.command=='enqueue':
                queue.enqueue(args.job,daily=args.daily,confirmations=max(1,args.confirmations))
            elif args.command=='invalidate':
                with commit_lock(config):
                    queue.invalidate(args.job,'manual-'+str(uuid.uuid4()))
            elif args.command=='retry':
                with commit_lock(config):
                    queue.retry(args.job)
            if args.command in ('invalidate','retry'):
                queue.record_manual_intervention()
            status=queue.status()
            # Only tick owns the aggregate status file and heartbeat.
            print(json.dumps(status))
        finally:
            queue.close()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
