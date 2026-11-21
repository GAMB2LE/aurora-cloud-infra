#!/usr/bin/env python3
"""Refuse a template install over a storage-guarded Python entrypoint."""
import ast
import os
from pathlib import Path
import stat
import sys


def require_plain_entrypoint(path, *, owner=0):
    path = Path(path)
    if not os.path.lexists(path):
        return
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            raise ValueError(f'{path}: expected protected, root-owned program')
        code = source.read(1024 * 1024 + 1)
    if len(code) > 1024 * 1024:
        raise ValueError(f'{path}: program exceeds the inspection limit')
    for node in ast.walk(ast.parse(code)):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = (target.id if isinstance(target, ast.Name) else
                target.attr if isinstance(target, ast.Attribute) else
                target.slice.value if isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant) else None)
        if name == 'run_python_original':
            raise ValueError(f'{path}: storage-guarded program; use a pinned-payload release, preserving its wrapper')


if __name__ == '__main__':
    try:
        require_plain_entrypoint(sys.argv[1])
    except (OSError, ValueError, SyntaxError, IndexError) as error:
        raise SystemExit(str(error))
