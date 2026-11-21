"""Reconcile a finalized GWS operation in its own durable queue.

The caller must hold both archive writer locks and authenticate any other
outstanding worker before invoking this driver. A launcher crash or missing
receipt remains outstanding; this module never starts a second attempt.
"""
import json
from pathlib import Path
import re

from bounded_backlog_runner import admission, launch, state, successful
from capture_gws_only import capture, finalized_argv
from gws_only_runner import exclusive_json
from gws_pending_ledger import register_captured
from per_file_coordinator import AUTHORITY, BASE, PENDING, candidate_identity


class FinalizedGwsDriver:
    def __init__(self, preparation=None):
        self.preparation = preparation

    def reserve(self, expected, target):
        if target != 'gws-verified' or not isinstance(self.preparation, dict):
            raise ValueError('finalized driver only reserves a prepared GWS transfer')
        out = Path(self.preparation['out'])
        if out.parent != BASE or candidate_identity(out) != expected:
            raise ValueError('finalized preparation differs from expected version')
        request_raw = (out / 'request.json').read_bytes()
        request = json.loads(request_raw)
        if request.get('finalized_stage', {}).get('expected') != expected:
            raise ValueError('finalized handoff differs from queued version')
        finalized_argv(out, request_raw)
        stored = json.loads((out / 'admission-evidence.json').read_bytes())
        unit = stored.get('unit')
        if (not isinstance(unit, str) or not re.fullmatch(r'aurora-finalized-admission-20\d{6}t\d{6}z\.service', unit)
                or stored.get('retention_authorized') is not False or stored.get('eviction_authorized') is not False
                or admission(unit, request) != {'accepted': stored.get('accepted'), 'invocation_id': stored.get('invocation_id')}):
            raise ValueError('finalized read-only admission differs')
        operation = self.preparation['operation']
        if (not isinstance(operation, str) or not re.fullmatch(r'20261027-gws-only-20\d{6}t\d{6}z-0', operation)
                or request.get('operation_id') != operation):
            raise ValueError('worker operation differs from request')
        return {'preparation': self.preparation, 'unit': 'aurora-direct-' + operation + '.service'}

    def apply(self, op):
        intent = op['intent']
        prep = intent['preparation']
        if self.reserve(op['expected'], op['target']) != intent:
            raise ValueError('durable operation differs from preparation')
        out = Path(prep['out'])
        exclusive_json(out / 'launch-intent.json', {'unit': intent['unit'],
                       'relative_path': op['expected']['relative_path'], **AUTHORITY})
        identity = launch(intent['unit'], out)
        exclusive_json(out / 'service-identity.json', identity)

    def observe(self, op):
        if op.get('target') != 'gws-verified':
            raise ValueError('finalized driver cannot reconcile another stage')
        out = Path(op['intent']['preparation']['out'])
        if candidate_identity(out) != op['expected']:
            raise ValueError('finalized request changed')
        verification = out / 'captured-gws-evidence' / 'verification.json'
        if not verification.is_file():
            identity_path = out / 'service-identity.json'
            if not identity_path.is_file(): return None
            identity = json.loads(identity_path.read_bytes())
            props = state(op['intent']['unit'])
            if (identity.get('unit') != op['intent']['unit'] or props.get('InvocationID') != identity.get('invocation_id')
                    or props.get('ExecMainPID') != str(identity.get('main_pid'))):
                raise ValueError('recorded GWS worker identity differs')
            if props.get('ActiveState') in ('failed', 'inactive'):
                raise RuntimeError('finalized GWS attempt requires reconciliation')
            if not successful(props): return None
            capture(out)
        result = register_captured(out, PENDING)
        return {'operation_id': op['id'], 'expected': op['expected'], 'stage': 'gws-verified',
                'verified': True, 'evidence': {'pending_evidence_sha256': result['evidence_sha256']}, **AUTHORITY}
