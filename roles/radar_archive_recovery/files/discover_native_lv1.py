"""Find recent native LV1 predecessor/successor pairs for later verification.

Discovery alone is not a closed-file, staging, publication or retention proof.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat

from windows_native_probe import NAME, validate_pair


def discover(root, *, now=None, days=3):
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None or type(days) is not int or not 1 <= days <= 7:
        raise ValueError('invalid discovery window')
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('invalid source root')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    records = []
    try:
        for offset in range(days):
            date = (now - timedelta(days=offset)).date()
            parts = (f'Y{date.year:04}', f'M{date.month:02}', f'D{date.day:02}')
            opened = []
            try:
                parent = fd
                for part in parts:
                    parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    opened.append(parent)
                for name in os.listdir(parent):
                    relative = '/'.join((*parts, name))
                    match = NAME.fullmatch(relative)
                    if not match or match.group(7) != 'LV1': continue
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0: continue
                    try:
                        stamp = datetime.strptime(match.group(4) + match.group(5), '%y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    if stamp.date() != date or stamp > now: continue
                    records.append((stamp, match.group(6), relative))
            except FileNotFoundError:
                pass
            finally:
                for child in reversed(opened): os.close(child)
    finally:
        os.close(fd)
    groups = {}
    for stamp, programme, relative in records:
        groups.setdefault(programme, []).append((stamp, relative))
    result = []
    for programme, items in groups.items():
        items.sort()
        for (stamp, source), (_, successor) in zip(items, items[1:]):
            validate_pair(source, successor)
            result.append({'source': source, 'successor': successor, 'source_time': stamp.isoformat(),
                           'programme': programme, 'staging_authorized': False,
                           'retention_authorized': False, 'eviction_authorized': False})
    return sorted(result, key=lambda item: item['source_time'], reverse=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(discover('/home/aurora/data/rpgfmcw94', days=args.days), sort_keys=True))


if __name__ == '__main__': main()
