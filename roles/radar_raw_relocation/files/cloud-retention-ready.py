#!/usr/bin/env python3
"""Admit cloud retention only after the rehearsed release and clean restoration."""
import hashlib
import json
from pathlib import Path

release = Path('/var/lib/aurora-retention-return-20261121/layout-candidate-v2/release.json')
for row in json.loads(release.read_text())['changes']:
    if hashlib.sha256(Path(row['path']).read_bytes()).hexdigest() != row['after_sha256']:
        raise SystemExit(1)
base = Path('/var/lib/aurora-cloud/radar-retention-windows')
name = json.loads((base / 'current.json').read_text())['run']
if Path(name).name != name:
    raise SystemExit(1)
state = json.loads((base / name / 'state.json').read_text())
if not state.get('restored_at') or not state.get('safe_to_resume'):
    raise SystemExit(1)
if Path('/etc/aurora/radar-relocation.hold').exists():
    raise SystemExit(1)
