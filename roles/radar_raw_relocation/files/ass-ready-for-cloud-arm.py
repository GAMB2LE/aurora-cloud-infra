#!/usr/bin/env python3
"""Arm the initial cloud calendar only after the strict scheduled ASS check."""
import json
from pathlib import Path

base=Path('/var/lib/aurora-cloud/retention-windows')
name=json.loads((base/'current.json').read_text())['run']
if Path(name).name!=name:raise SystemExit(1)
state=json.loads((base/name/'state.json').read_text())
rows=state.get('candidates',[])
ready=(bool(rows) and state.get('max_candidates')==5000
       and state.get('receipt_audit_complete') is True
       and state.get('source_absence_verified') is True
       and state.get('source_paths_absent')==len(rows)
       and state.get('independent_edge_audit',{}).get('edge_receipts_match') is True
       and state.get('signed_result',{}).get('Result')=='success'
       and bool(state.get('restored_at')))
raise SystemExit(0 if ready else 1)
