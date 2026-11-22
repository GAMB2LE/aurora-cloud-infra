#!/usr/bin/env python3
"""Build a reviewable layout release and refresh exact dependency pins, offline."""
import argparse
import hashlib
import json
from pathlib import Path

from jinja2 import Template

COHORT = Path('var/lib/aurora-radar-coordinator-v1/overnight-20261114/relocated-may-d13-cutover-candidate')
HELPER = Path('usr/local/lib/aurora-radar-retention-layout.py')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('expected exactly one reviewed source fragment: ' + old[:90])
    return text.replace(old, new)


def build(source, output, repository):
    if output.exists():
        raise ValueError('release output already exists; preserve it for review')
    originals = {p.relative_to(source): p.read_text() for p in source.rglob('*.py')
                 if '.before' not in p.name and p.name != 'fix_finalized_layout_launcher.py'}
    current = dict(originals)
    helper = (repository / 'roles/radar_raw_relocation/files/aurora-radar-retention-layout.py').read_text()
    current[HELPER] = helper
    current[COHORT / 'verify_radar_mount_layout.py'] = Template(
        (repository / 'roles/radar_raw_relocation/templates/verify_radar_mount_layout.py.j2').read_text()
    ).render(retention_layout_sha256=digest(helper)) + '\n'

    continuity_path = COHORT / 'verify_released_radar_continuity.py'
    text = originals[continuity_path]
    text = replace_once(text,
        'expected_bytes=608272110490, root_owned=True, verify_replacement=None)',
        'expected_bytes=608272110490, root_owned=True, verify_replacement=None, replacement_days=None)')
    text = replace_once(text, 'replacement = day == COMPRESSED_DAY and verify_replacement is not None',
        'replacement = day in (replacement_days or {COMPRESSED_DAY}) and verify_replacement is not None')
    text = replace_once(text, '            replacement = None\n',
        '            replacement = None\n            replacement_days = {COMPRESSED_DAY}\n')
    text = replace_once(text, '                def replacement():\n',
        "                initial_layout = layout['verify_layout'](route=route)\n"
        "                replacement_days.update(initial_layout.get('retention_compressed_routes', {}))\n"
        '                def replacement():\n')
    text = replace_once(text,
        "require(state['legacy_bind_days'] == 6 and state['compressed_route']['files'] == 64,",
        "require(state['legacy_bind_days'] + len(state.get('retention_compressed_routes', {})) == 6 "
        "and state['compressed_route']['files'] == 64 "
        "and set(state.get('retention_compressed_routes', {})) == replacement_days - {COMPRESSED_DAY},")
    text = replace_once(text, 'verify_replacement=replacement)',
        'verify_replacement=replacement, replacement_days=replacement_days)')
    text = replace_once(text, "                result['compressed_day'] = COMPRESSED_DAY\n",
        "                result['retention_compressed_days'] = sorted(replacement_days - {COMPRESSED_DAY})\n"
        "                result['compressed_day'] = COMPRESSED_DAY\n")
    # Bind the additional root-owned route descriptor into the existing stable
    # control checks; historical manifests and acceptance bytes remain untouched.
    text = replace_once(text, "COMPRESSED_ROUTE = Path('/etc/aurora/radar-may-d13-compressed.json')",
        "RETENTION_REGISTRY = Path('/etc/aurora/radar-retention-compressed.json')\n"
        "COMPRESSED_ROUTE = Path('/etc/aurora/radar-may-d13-compressed.json')")
    text = replace_once(text,
        'controls = {path: stable_read(path) for path in (HELPER, CONFIG, ACTIVE, RETENTION)}',
        'controls = {path: stable_read(path) for path in (HELPER, CONFIG, ACTIVE, RETENTION,\n'
        '                *((RETENTION_REGISTRY,) if os.path.lexists(RETENTION_REGISTRY) else ()))}')
    text = replace_once(text,
        'require(all(stable_read(path) == previous for path, previous in controls.items())',
        'require((RETENTION_REGISTRY in controls) == os.path.lexists(RETENTION_REGISTRY)\n'
        '                    and all(stable_read(path) == previous for path, previous in controls.items())')
    current[continuity_path] = text

    old_assert = "json.loads(result)=={'compressed_route': {'files': 64, 'image_sha256': '059debe210ae32f3ce2735d454c4255765c525a7dd0e432158711e7f24471d2f', 'verified_day': 'Y2026/M05/D13'}, 'legacy_bind_days': 6}"
    new_assert = "(json.loads(result).get('compressed_route') == {'files': 64, 'image_sha256': '059debe210ae32f3ce2735d454c4255765c525a7dd0e432158711e7f24471d2f', 'verified_day': 'Y2026/M05/D13'} and json.loads(result).get('legacy_bind_days', -1) + len(json.loads(result).get('retention_compressed_routes', {})) == 6)"
    replacements = 0
    for path, raw in list(current.items()):
        if old_assert in raw:
            current[path] = replace_once(raw, old_assert, new_assert)
            replacements += 1
    if replacements != 3:
        raise ValueError('verified publisher coverage assertions changed: ' + str(replacements))

    # Refresh only references to actual original component hashes, never broad
    # strings or historical manifests. Iterate because callers pin their callees.
    for iteration in range(20):
        mapping = {digest(old): digest(current[path]) for path, old in originals.items()
                   if current[path] != old}
        updated = {}
        for path, raw in current.items():
            value = raw
            for old, new in mapping.items():
                value = value.replace(old, new)
            updated[path] = value
        if updated == current:
            break
        current = updated
    else:
        raise ValueError('dependency pins do not form a stable finite release')

    changes = []
    for path, raw in current.items():
        compile(raw, str(path), 'exec')
        if originals.get(path) == raw:
            continue
        target = output / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(raw)
        changes.append({'path': '/' + str(path), 'before_sha256': digest(originals[path]) if path in originals else None,
                        'after_sha256': digest(raw)})
    (output / 'release.json').write_text(json.dumps({'schema': 'radar-retention-layout-release-v1',
        'historical_manifests_modified': False, 'science_paths_modified': False,
        'changes': changes}, indent=2) + '\n')
    return changes


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build(args.source, args.output, Path(__file__).resolve().parents[1])
    print(json.dumps({'changed_components': len(result), 'paths': [r['path'] for r in result]}, indent=2))
