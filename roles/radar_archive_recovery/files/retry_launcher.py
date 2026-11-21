"""Build an operation-bound retry launcher from the pinned deployed template."""
import ast
import hashlib
import json
import re


def build(*, template, template_sha256, original_stamp, original_worker,
          preflight_raw, journal_raw, original_request, operation_id, attempt,
          release_launcher_sha256=None):
    """Return new launcher/request bytes without changing the original stage.

    The caller must independently authenticate the deployed release and all
    supplied inputs and obtain the template digest from the pinned release.
    """
    if (not isinstance(template, bytes) or not re.fullmatch('[0-9a-f]{64}', template_sha256)
            or hashlib.sha256(template).hexdigest() != template_sha256):
        raise ValueError('resume template differs from the pinned release')
    if not (isinstance(original_stamp, str) and re.fullmatch(r'20\d{6}T\d{6}Z', original_stamp)
            and isinstance(operation_id, str) and re.fullmatch(r'[0-9a-f]{32}', operation_id)
            and type(attempt) is int and 1 <= attempt <= 999999):
        raise ValueError('invalid recovery identity')
    worker = 'aurora-direct-20261027-gws-only-' + original_stamp.lower() + '-0.service'
    if (not isinstance(original_worker, dict) or original_worker.get('unit') != worker
            or not re.fullmatch('[0-9a-f]{32}', original_worker.get('invocation_id', ''))
            or type(original_worker.get('main_pid')) is not int or original_worker['main_pid'] <= 0):
        raise ValueError('original staged worker identity differs')
    if not (isinstance(preflight_raw, bytes) and isinstance(journal_raw, bytes)
            and isinstance(original_request, dict)):
        raise ValueError('missing original recovery evidence')
    try:
        preflight = json.loads(preflight_raw)
        events = [json.loads(line) for line in journal_raw.splitlines()]
    except (TypeError, ValueError):
        raise ValueError('invalid staged evidence') from None
    expected = preflight.get('expected', {})
    source = original_request.get('expected_source', {})
    source_identity = source.get('identity', []) if isinstance(source, dict) else []
    if (not isinstance(expected, dict) or set(expected) != {'relative_path', 'size', 'mtime_ns', 'sha256'}
            or preflight.get('schema') != 'radar-direct-backlog-preflight-v1'
            or original_request.get('relative_path') != expected.get('relative_path')
            or not isinstance(source_identity, list) or len(source_identity) != 10
            or source_identity[2] != expected.get('size') or source_identity[3] != expected.get('mtime_ns')
            or source.get('sha256') != expected.get('sha256')
            or [event.get('phase') for event in events] != ['intent', 'stage-complete']
            or not all(all(event.get(k) == v for k, v in expected.items()) for event in events)
            or events[0].get('operation') != preflight.get('operation')
            or events[1].get('source_stable') is not True
            or not all(item.get('retention_authorized') is False and item.get('eviction_authorized') is False
                       for item in [preflight, *events])):
        raise ValueError('request or journal differs from original stage')
    retry_id = f'retry-{operation_id}-{attempt:06d}'
    replacements = {
        '20261027-gws-only-103414z-0': '20261027-gws-only-' + original_stamp.lower() + '-0',
        '20261027-gws-resume-103414z-0': retry_id,
        '7368ca8b53254d2f8effa6ad123ee6df': original_worker['invocation_id'],
        '897345': str(original_worker['main_pid']),
        '8b9b1d08a732f14095589b49c8528c1af15e9bfa24293eaeb34ae7f611165092': hashlib.sha256(preflight_raw).hexdigest(),
        'caee1fc22e844d0a54c0e2dd9cf752b79376b335aff6db6f414cb09c71d71e1f': hashlib.sha256(journal_raw).hexdigest(),
    }
    text = template.decode('utf-8')
    if 'VM01_LAUNCHER_SHA256_PLACEHOLDER' in text:
        if not isinstance(release_launcher_sha256, str) or not re.fullmatch('[0-9a-f]{64}', release_launcher_sha256):
            raise ValueError('vm01 original launcher pin missing')
        replacements['VM01_LAUNCHER_SHA256_PLACEHOLDER'] = release_launcher_sha256
    for old, new in replacements.items():
        if old not in text:
            raise ValueError('pinned resume template lacks required field')
        text = text.replace(old, new)
    ast.parse(text)
    request = dict(original_request)
    request['operation_id'] = retry_id
    return {'launcher': text.encode(), 'request': json.dumps(request, sort_keys=True).encode(),
            'unit': 'aurora-direct-' + retry_id + '.service', 'expected': expected}


def build_readonly_admission(launcher):
    """Instrument the pinned retry launcher to return its independent proofs.

    The generated program refuses --apply and invokes the existing pinned
    staged-recovery module in read-only mode. A caller must authenticate its
    service identity, launcher digest and complete output before using proofs.
    """
    if not isinstance(launcher, bytes):
        raise ValueError('launcher bytes required')
    text = launcher.decode('utf-8')
    marker = "    preflight_raw, journal_raw = base.control(Path(str(STEM) + '.preflight.json')), base.control(STEM)"
    instrumentation = '''        def hash_settled_source(self, path, *, timeout):
            proof = super().hash_settled_source(path, timeout=timeout)
            self._retry_source_proof = proof
            return proof

        def verify_staged(self, stage, *, timeout):
            proof = super().verify_staged(stage, timeout=timeout)
            self._retry_stage_proof = proof
            return proof

'''
    call = '    result = resume.resume(adapter, observation, preflight, journal_raw, STEM, apply=args.apply)'
    replacement = '''    if args.apply:
        raise ValueError('admission launcher is read-only')
    result = resume.resume(adapter, observation, preflight, journal_raw, STEM, apply=False)
    source = adapter._retry_source_proof
    result['source_proof'] = {'relative_path': source['relative_path'],
                              'size': source['identity'][2], 'mtime_ns': source['identity'][3],
                              'sha256': source['sha256'], 'stable': True}
    result['stage_proof'] = adapter._retry_stage_proof
    result['gws_absent'] = True
    result['holds_ok'] = True
    result['capacity_ok'] = True'''
    if text.count(marker) != 1 or text.count(call) != 1:
        raise ValueError('pinned retry launcher shape differs')
    text = text.replace(marker, instrumentation + marker).replace(call, replacement)
    ast.parse(text)
    return text.encode()


def build_capture(*, template, template_sha256, original_stamp, preflight_raw,
                  journal_raw, operation_id, attempt):
    """Bind the existing evidence capture to the successful retry unit."""
    if (not isinstance(template, bytes) or not isinstance(template_sha256, str)
            or not re.fullmatch('[0-9a-f]{64}', template_sha256)
            or hashlib.sha256(template).hexdigest() != template_sha256):
        raise ValueError('capture template differs from pinned release')
    if not (isinstance(original_stamp, str) and re.fullmatch(r'20\d{6}T\d{6}Z', original_stamp)
            and isinstance(operation_id, str) and re.fullmatch('[0-9a-f]{32}', operation_id)
            and type(attempt) is int and 1 <= attempt <= 999999
            and isinstance(preflight_raw, bytes) and isinstance(journal_raw, bytes)):
        raise ValueError('invalid capture identity')
    retry_id = f'retry-{operation_id}-{attempt:06d}'
    replacements = {
        '20261107T103414Z': original_stamp,
        '20261027-gws-only-103414z-0': '20261027-gws-only-' + original_stamp.lower() + '-0',
        '20261027-gws-resume-103414z-0': retry_id,
        '8b9b1d08a732f14095589b49c8528c1af15e9bfa24293eaeb34ae7f611165092': hashlib.sha256(preflight_raw).hexdigest(),
        'caee1fc22e844d0a54c0e2dd9cf752b79376b335aff6db6f414cb09c71d71e1f': hashlib.sha256(journal_raw).hexdigest(),
        '/resume_stage_launcher_103414.py': '/' + retry_id + '/launcher.py',
        'from capture_gws_only import BASE, REMOTE, save': 'from capture_gws_only_refresh import BASE, REMOTE, save',
        'out.mkdir(mode=0o700)': 'out.mkdir(mode=0o700, exist_ok=True)',
    }
    text = template.decode('utf-8')
    for old, new in replacements.items():
        if old not in text:
            raise ValueError('pinned capture template lacks required field')
        text = text.replace(old, new)
    ast.parse(text)
    return text.encode()


def admission_proofs(raw, expected):
    """Parse authenticated read-only worker output without inferring success."""
    if not isinstance(raw, bytes) or not raw or len(raw) > 1024 * 1024:
        raise ValueError('admission output missing or oversized')
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('admission output is not one JSON record') from None
    if not isinstance(value, dict) or not isinstance(expected, dict):
        raise ValueError('invalid admission output')
    if not (value.get('mode') == 'read-only' and value.get('stage_verified') is True
            and value.get('gws_verified') is False and value.get('object_verified') is False
            and value.get('retention_authorized') is False and value.get('eviction_authorized') is False
            and value.get('gws_absent') is True and value.get('holds_ok') is True and value.get('capacity_ok') is True):
        raise ValueError('read-only admission failed')
    source, stage = value.get('source_proof'), value.get('stage_proof')
    if not (isinstance(source, dict) and source.get('stable') is True
            and all(source.get(key) == field for key, field in expected.items())
            and isinstance(stage, dict) and stage.get('schema') == 'radar-direct-gws-stage-verified-v1'
            and stage.get('sha256') == expected.get('sha256') and stage.get('size') == expected.get('size')
            and stage.get('retention_authorized') is False and stage.get('eviction_authorized') is False):
        raise ValueError('admission version differs')
    return {key: value[key] for key in ('source_proof', 'stage_proof', 'gws_absent', 'holds_ok', 'capacity_ok')}
