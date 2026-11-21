#!/usr/bin/env python3
"""Pinned launcher for the 2026-11-07 interrupted GWS stage.

This launcher must itself be installed root-owned and pinned by its invoker.
It handles exactly one recorded attempt, and cannot authorize data release.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import types

ROOT = Path('/var/lib/aurora-recovery/repair-20261026/archive-first-implementation-20261027')
NEW = ROOT / 'direct-staged-v1'
ORIGINAL = ROOT / 'direct-staged-gzip-v4-vm01' / 'direct_staged_launcher_gzip.py'
ORIGINAL_SHA = 'VM01_LAUNCHER_SHA256_PLACEHOLDER'
WORKER = {'unit': 'aurora-direct-20261027-gws-only-103414z-0.service',
          'invocation_id': '7368ca8b53254d2f8effa6ad123ee6df', 'main_pid': 897345}
STEM = NEW / 'transfer-20261027-gws-only-103414z-0.jsonl'
PREFLIGHT_SHA = '8b9b1d08a732f14095589b49c8528c1af15e9bfa24293eaeb34ae7f611165092'
JOURNAL_SHA = 'caee1fc22e844d0a54c0e2dd9cf752b79376b335aff6db6f414cb09c71d71e1f'
RESUME_UNIT = 'aurora-direct-20261027-gws-resume-103414z-0.service'


def load_staged_component(original, base, name):
    if name == 'direct_stage_pipeline':
        return original.load(base, original.LONG / (name + '.py'), '45e81ded6b9296f20fc20724991d6156aa03dccd6115d314558e93bb074abffb', name)
    root = original.LONG if name in ('gws_fence_cloud_adapter', 'direct_staged_cloud_adapter') else NEW
    return original.load(base, root / (name + '.py'), original.PINS[name + '.py'], name)


def main():
    p = argparse.ArgumentParser()
    for name in ('request', 'continuity', 'observation'):
        p.add_argument('--' + name, type=Path, required=True)
        p.add_argument('--' + name + '-sha256', required=True)
    p.add_argument('--resume-module-sha256', required=True)
    p.add_argument('--worker-module-sha256', required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    if os.geteuid() != 0:
        raise ValueError('run as root')
    # Initial bootstrap is pinned and checks all ancestor ownership itself.
    raw = ORIGINAL.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ORIGINAL_SHA:
        raise ValueError('launcher pin differs')
    original = types.ModuleType('original_stage_launcher')
    original.__file__ = str(ORIGINAL)
    exec(compile(raw, str(ORIGINAL), 'exec'), original.__dict__)
    base = original.bootstrap()
    base.protected(NEW)
    if base.digest(base.control(ORIGINAL)) != ORIGINAL_SHA:
        raise ValueError('protected launcher differs')
    old = original.load(base, original.OLD / 'landed_recovery_launcher.py', original.OLD_LAUNCHER_SHA, 'staged_old_launcher')
    inspector = original.load(base, old.INSPECTOR, old.PINS[old.INSPECTOR], 'staged_inspector')
    for name in ('landed_receipt_validation', 'landed_recovery_journal', 'landed_archive_absence', 'landed_cloud_adapter'):
        path = original.OLD / (name + '.py')
        original.load(base, path, old.PINS[path], name)
    for name in ('gws_receive', 'landed_gws_cloud_adapter'):
        original.load(base, original.DIRECT / (name + '.py'), original.DIRECT_PINS[name + '.py'], name)
    for name in ('direct_capacity', 'direct_interrupt', 'direct_stage_pipeline', 'direct_authorize',
                 'gws_publication_fence', 'gws_fence_recovery', 'gws_fence_cloud_adapter',
                 'direct_staged_commit', 'direct_backlog_cloud_adapter', 'direct_staged_cloud_adapter', 'direct_staged_recovery'):
        load_staged_component(original, base, name)
    for name in ('gws_staged_receive.py', 'remote_commit_handshake.py', 'remote_process_probe.py'):
        base.require(base.digest(base.control(NEW / name)) == original.PINS[name], 'transport pin differs')
    worker_check = original.load(base, NEW / 'verify_failed_stage_worker.py', args.worker_module_sha256, 'verify_failed_stage_worker')
    resume = original.load(base, NEW / 'resume_verified_gws_stage.py', args.resume_module_sha256, 'resume_verified_gws_stage')
    delivery = original.load(base, original.HELPER, original.HELPER_SHA, 'installed_fenced_delivery')
    def pinned_input(path, digest):
        base.protected(path.parent)
        data = base.control(path)
        base.require(base.digest(data) == digest, 'input pin differs')
        return json.loads(data)
    request = pinned_input(args.request, args.request_sha256)
    observation = pinned_input(args.observation, args.observation_sha256)
    pinned_input(args.continuity, args.continuity_sha256)
    base.require(request['operation_id'] == '20261027-gws-resume-103414z-0', 'wrong recovery operation')
    if args.apply:
        original.require_owned_service(base, request['operation_id'])
    wrapper, relocation = base.control(base.WRAPPER), base.control(base.RELOCATION)
    base.require(base.digest(wrapper) == request['wrapper_sha256'] and base.digest(relocation) == request['relocation_helper_sha256'], 'controls differ')
    helper = types.ModuleType('staged_relocation')
    exec(compile(relocation, str(base.RELOCATION), 'exec'), helper.__dict__)
    cfg = base.config(wrapper)
    cfg['service_uid'] = pwd.getpwnam('aurora').pw_uid
    probe = base.control(NEW / 'remote_process_probe.py')
    fence = sys.modules['gws_fence_cloud_adapter'].FenceCloudAdapter(base, inspector, cfg['service_uid'], probe)
    parent = sys.modules['direct_staged_cloud_adapter'].StagedCloudAdapter
    class Adapter(parent):
        def require_original_worker_terminal(self):
            fields = ('LoadState', 'ActiveState', 'SubState', 'Result', 'ExecMainStatus', 'ExecMainPID', 'InvocationID', 'ControlGroup')
            result = subprocess.run(['systemctl', 'show', WORKER['unit'], *['-p' + x for x in fields]],
                                    capture_output=True, text=True, check=True, timeout=30)
            properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            group = properties.get('ControlGroup', '')
            if group and (not group.startswith('/system.slice/') or '..' in group):
                raise ValueError('invalid cgroup')
            pids = [int(x) for x in (Path('/sys/fs/cgroup') / group.lstrip('/') / 'cgroup.procs').read_text().split()] if group else []
            artifacts = {'commit': os.path.lexists(str(STEM) + '.commit.jsonl'),
                         'fence_receipt': os.path.lexists(str(STEM) + '.fence-reconciliation.json'),
                         'readback': os.path.lexists(str(STEM) + '.readback.json'),
                         'fence': os.path.lexists('/etc/aurora/gws-direct-publication.fence')}
            worker_check.verify(WORKER, properties, pids, preflight_raw, journal_raw, artifacts)
    preflight_raw, journal_raw = base.control(Path(str(STEM) + '.preflight.json')), base.control(STEM)
    base.require(base.digest(preflight_raw) == PREFLIGHT_SHA and base.digest(journal_raw) == JOURNAL_SHA, 'original evidence pin differs')
    preflight = json.loads(preflight_raw)
    base.require(request['relative_path'] == preflight['expected']['relative_path'], 'recovery path differs')
    adapter = Adapter(base, inspector, helper, cfg, args.request, args.request_sha256, args.continuity,
                      args.continuity_sha256, delivery_helper=delivery, fence_recovery_adapter=fence)
    result = resume.resume(adapter, observation, preflight, journal_raw, STEM, apply=args.apply)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
