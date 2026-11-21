"""Sequential GWS-only operations; stop on first uncertainty, never retry."""
import json
import os
from pathlib import Path

from bounded_backlog_sequence import PATH


def run(paths, adapter, journal):
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 20 or len(set(paths)) != len(paths)
            or any(not isinstance(p, str) or not PATH.fullmatch(p) for p in paths)):
        raise ValueError('invalid GWS-only batch')
    journal = Path(journal)
    fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    def event(phase, **extra):
        raw = (json.dumps({'phase': phase, 'retention_authorized': False,
                           'eviction_authorized': False, **extra}, sort_keys=True) + '\n').encode()
        while raw:
            n = os.write(fd, raw)
            if n <= 0: raise OSError('short journal write')
            raw = raw[n:]
        os.fsync(fd)
    completed = 0
    try:
        parent = os.open(journal.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(parent)
        finally: os.close(parent)
        event('batch-start', count=len(paths))
        for path in paths:
            event('item-intent', relative_path=path)
            for step in ('probe', 'prepare', 'admit', 'transfer', 'capture'):
                event('step-intent', relative_path=path, step=step)
                evidence = getattr(adapter, step)(path)
                if not isinstance(evidence, dict) or evidence.get('accepted') is not True:
                    raise ValueError('GWS-only step refused: ' + step)
                event('step-complete', relative_path=path, step=step, evidence=evidence)
            completed += 1
            event('item-complete', relative_path=path)
        event('batch-complete', completed=completed)
        return completed
    except BaseException as exc:
        event('batch-stopped', completed=completed, error_type=type(exc).__name__)
        raise
    finally:
        os.close(fd)
