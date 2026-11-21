"""Validate terminal identity and records of an interrupted staged worker.

The caller authenticates the files and obtains systemd and cgroup state from
the host. A positive result does not authorize publication or deletion.
"""
import hashlib
import json
import re


def verify(identity, properties, cgroup_pids, preflight_raw, journal_raw, artifacts):
    if not (isinstance(identity, dict)
            and re.fullmatch(r'aurora-direct-[a-z0-9-]+\.service', identity.get('unit', ''))
            and re.fullmatch(r'[0-9a-f]{32}', identity.get('invocation_id', ''))
            and type(identity.get('main_pid')) is int and identity['main_pid'] > 0):
        raise ValueError('invalid worker identity')
    if not (properties.get('LoadState') == 'loaded' and properties.get('ActiveState') == 'failed'
            and properties.get('SubState') == 'failed' and properties.get('Result') == 'exit-code'
            and properties.get('ExecMainStatus') == '1'
            and properties.get('ExecMainPID') == str(identity['main_pid'])
            and properties.get('InvocationID') == identity['invocation_id'] and cgroup_pids == []):
        raise ValueError('worker is not the recorded terminal instance')
    if artifacts != {'commit': False, 'fence_receipt': False, 'readback': False, 'fence': False}:
        raise ValueError('publication artifacts require reconciliation')
    preflight = json.loads(preflight_raw)
    events = [json.loads(x) for x in journal_raw.splitlines()]
    expected = preflight.get('expected', {})
    if not (preflight.get('schema') == 'radar-direct-backlog-preflight-v1'
            and len(events) == 2 and [e.get('phase') for e in events] == ['intent', 'stage-complete']
            and events[0].get('operation') == preflight.get('operation')
            and set(expected) == {'relative_path', 'size', 'mtime_ns', 'sha256'}
            and all(e.get(k) == v for e in events for k, v in expected.items())
            and all(e.get('eviction_authorized') is False and e.get('retention_authorized') is False for e in [preflight, *events])
            and events[1].get('source_stable') is True
            and all(events[1].get('stage_receipt', {}).get(k) == v for k, v in expected.items())
            and events[1]['stage_receipt'].get('nonce') == preflight.get('operation', {}).get('nonce')):
        raise ValueError('staging evidence differs')
    return {'state': 'terminal-staged-unpublished', 'preflight_sha256': hashlib.sha256(preflight_raw).hexdigest(),
            'journal_sha256': hashlib.sha256(journal_raw).hexdigest(), 'identity': identity,
            'expected': expected, 'eviction_authorized': False, 'retention_authorized': False}
