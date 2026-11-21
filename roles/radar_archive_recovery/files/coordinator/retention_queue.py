"""Rank a finite mirror observation for guarded repair; never authorizes copying."""
import csv
import io
import re

PATH = re.compile(r'Y\d{4}/M\d{2}/D\d{2}/[A-Za-z0-9_.-]+\.(?:LV0|LV0\.NC|LV1|LV1\.NC)\Z')

def parse_manifest(raw, *, allow_other=False):
    if not isinstance(raw, bytes) or type(allow_other) is not bool or len(raw) > 16 * 1024 * 1024:
        raise ValueError('invalid manifest size')
    rows = csv.DictReader(io.StringIO(raw.decode('utf-8')), delimiter='\t')
    if rows.fieldnames != ['relpath', 'size', 'mtime', 'checksum']:
        raise ValueError('invalid manifest header')
    result = {}
    for row in rows:
        if set(row) != {'relpath', 'size', 'mtime', 'checksum'} or not all(isinstance(x, str) for x in row.values()):
            raise ValueError('malformed manifest row')
        path = row['relpath']
        safe_relative = (isinstance(path, str) and 0 < len(path) <= 4096
                         and not path.startswith('/') and all(part not in ('', '.', '..') for part in path.split('/'))
                         and not any(ord(char) < 32 for char in path))
        if (not safe_relative or (not allow_other and not PATH.fullmatch(path)) or path in result
                or not row['size'].isdigit() or not row['mtime'].isdigit()):
            raise ValueError('invalid or duplicate manifest row')
        result[path] = {'size': int(row['size']), 'mtime': int(row['mtime'])}
    return result

def rank(source, primary, gws, known_versions, *, cutoff, maximum):
    """Prioritize small, old, absent-both paths without guessing archive parity."""
    if type(cutoff) is not int or type(maximum) is not int or not 1 <= maximum <= 1000:
        raise ValueError('invalid selection bounds')
    if not isinstance(known_versions, set):
        raise ValueError('invalid exclusions')
    candidates = []
    for path, info in source.items():
        if (path not in primary and path not in gws and path not in known_versions
                and 0 < info['size'] <= 4 * 1024**3 and info['mtime'] < cutoff):
            candidates.append({'relative_path': path, **info})
    return sorted(candidates, key=lambda row: (row['size'], row['mtime'], row['relative_path']))[:maximum]
