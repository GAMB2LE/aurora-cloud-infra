"""Freeze source-observed native files absent from GWS for guarded backfill.

The metadata inventory is not a transfer proof. Each worker must recheck the
exact source, destination absence and controls before admitting any file.
"""
from datetime import date
import hashlib
import json
import re
import time

NAME = re.compile(r'rpgfmcw94-aurora_(\d{6})_(\d{6})_(P\d+_ZEN)\.(LV[01])\Z')


def build(raw, *, now=None):
    now = time.time() if now is None else now
    rows, dates = [], set()
    for line in raw.splitlines():
        day = json.loads(line)
        when = date.fromisoformat(day['date'])
        if day['date'] != when.isoformat() or day['date'] in dates:
            raise ValueError('invalid or duplicate source date')
        dates.add(day['date'])
        files = day['files']
        if not isinstance(files, list) or day.get('source_count') != len(files):
            raise ValueError('incomplete source inventory')
        for item in files:
            match = NAME.fullmatch(item.get('name', ''))
            if not match or match[1] != when.strftime('%y%m%d'):
                raise ValueError('source filename does not match its date')
            if (type(item.get('size')) is not int or item['size'] <= 0
                    or type(item.get('source_mtime')) is not int or item['source_mtime'] < 0):
                raise ValueError('invalid source metadata')
            if item.get('gws') != 'absent': continue
            if now - item['source_mtime'] < 21600: continue
            rows.append({'relative_path': f'Y{when:%Y}/M{when:%m}/D{when:%d}/{item["name"]}',
                         'size': item['size'], 'mtime': item['source_mtime']})
    if len({row['relative_path'] for row in rows}) != len(rows):
        raise ValueError('duplicate native source path')
    rows.sort(key=lambda row: (not row['relative_path'].endswith('.LV1'), -row['mtime'], row['relative_path']))
    return {'schema': 'radar-native-backfill-candidates-v1', 'candidate_count': len(rows), 'candidates': rows,
            'inventory_sha256': hashlib.sha256(raw).hexdigest(), 'dates': sorted(dates),
            'eviction_authorized': False, 'retention_authorized': False}
