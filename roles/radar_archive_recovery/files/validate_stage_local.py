#!/usr/bin/env python3
"""Run a pinned, resource-bounded LV1 structure check on protected ASS staging.

The caller starts this as a uniquely named systemd service with explicit memory
and runtime limits. The resulting proof does not authorize archive delivery.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

RELEASE = Path('/var/lib/aurora-radar-lv1-validator-v1')
INSPECT = Path('/var/lib/aurora-radar-fast-inspect-v1/inspect_protected_stage.py')
STAGE_ROOT = Path('/var/lib/aurora-radar-fast-staging')
NUMPY_BINARY = Path('/usr/lib/python3/dist-packages/numpy/core/_multiarray_umath.cpython-312-x86_64-linux-gnu.so')
PINS = {
    RELEASE / 'rpgpy/__init__.py': '9a4bcf6320c49f2460516de7de3cc0f385b4e4c72f9173704c424bc87a8c1dc3',
    RELEASE / 'radar_native_frames.py': 'c24ac703582c0c4e09d898bda0bc1ee7a31cc259f7d74ee80542b9213c8c110d',
    RELEASE / 'validate_native_lv1.py': '772e28ae1be01be95b279795dadd484b5aff5bfaaf648648d3c4eb64243c1bec',
    RELEASE / 'rpgpy/data.cpython-312-x86_64-linux-gnu.so': '022b2264e87702c157330b975b71ac2a382a5ccde38e80e4b9479e0d894c8ce2',
    RELEASE / 'rpgpy/header.py': '40fed038152f276bfd883010772acf6b93ae9c4d101c6263f79cb75c21940b3c',
    RELEASE / 'rpgpy/metadata.py': '49dccf0cf86dda9319bed6957c007b14028e04a834c509f8cbb8c3718b3e3360',
    RELEASE / 'rpgpy/utils.py': '86e6f50b4a7f2c9f55f64bfea98b54124177ec3bf73e66bdac7420e53bc10768',
    RELEASE / 'rpgpy/version.py': '367e945827b085358c96fbdfec1a6cc33ec7173a1c99dfac718ec8ecb24ecb1d',
    INSPECT: 'cdc709e0c34d68c61d1bb6544d93ea3d71beaf68185399a6f1079a7a432739f8',
    NUMPY_BINARY: 'ade966a58746fa5bc8e937a6c7d2d0a3b1e49adbb9890c3a3267157131f22313',
}


def protected_directory(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('validator directory is not protected')


def pin(path, expected):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_mode & 0o022
                or before.st_nlink != 1 or before.st_size > 32 * 1024**2):
            raise ValueError('validator component is not protected')
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
        after = os.fstat(stream.fileno())
    current = os.stat(path, follow_symlinks=False)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if (digest.hexdigest() != expected
            or any(getattr(before, key) != getattr(other, key) for other in (after, current) for key in fields)):
        raise ValueError('validator component changed or differs from its pin')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch('[0-9a-f]{32}', args.operation):
        raise ValueError('root and an exact stage operation are required')
    invocation = os.environ.get('INVOCATION_ID', '')
    unit = 'aurora-radar-lv1-validate-' + args.operation + '.service'
    if not re.fullmatch('[0-9a-f]{32}', invocation):
        raise ValueError('systemd invocation identity is missing')
    props = subprocess.run(['systemctl', 'show', unit, '-p', 'InvocationID', '-p', 'ExecMainPID', '-p', 'ExecStart', '-p', 'MemoryMax', '-p', 'RuntimeMaxUSec'],
                           capture_output=True, text=True, check=True, timeout=30).stdout
    properties = dict(line.split('=', 1) for line in props.splitlines() if '=' in line)
    if ('InvocationID=' + invocation not in props or 'ExecMainPID=' + str(os.getpid()) not in props
            or 'argv[]=' + shlex.join(['/usr/bin/python3', str(RELEASE / 'validate_stage_local.py'),
                                       '--operation', args.operation]) + ' ;' not in props):
        raise ValueError('systemd validation worker identity differs')
    if properties.get('MemoryMax') != '805306368' or properties.get('RuntimeMaxUSec') != '10min':
        raise ValueError('systemd validation limits differ from the reviewed limits')
    for directory in (Path('/var'), Path('/var/lib'), RELEASE, RELEASE / 'rpgpy', INSPECT.parent):
        protected_directory(directory)
    for path, digest in PINS.items(): pin(path, digest)
    for directory in (Path('/var'), Path('/var/lib'), RELEASE, RELEASE / 'rpgpy', INSPECT.parent):
        protected_directory(directory)
    import numpy
    import numpy.core._multiarray_umath as numpy_binary
    if numpy.__version__ != '1.26.4' or Path(numpy_binary.__file__) != NUMPY_BINARY:
        raise ValueError('NumPy runtime differs from the reviewed release')
    from inspect_protected_stage import inspect
    from validate_native_lv1 import validate
    proof, readback = inspect(STAGE_ROOT, args.operation)
    result = validate(proof['stage_path'], args.operation, proof['sha256'])
    current, current_readback = inspect(STAGE_ROOT, args.operation)
    if proof != current or readback != current_readback:
        raise ValueError('protected stage changed during validation')
    for path, digest in PINS.items(): pin(path, digest)
    result.update({'worker_unit': unit, 'worker_invocation_id': invocation, 'worker_pid': os.getpid(),
                   'numpy_version': numpy.__version__, 'numpy_binary_sha256': PINS[NUMPY_BINARY]})
    directory = os.open(STAGE_ROOT / args.operation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open('structure-validation.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        raw = (json.dumps(result, sort_keys=True, separators=(',', ':')) + '\n').encode()
        with os.fdopen(fd, 'wb') as stream:
            if stream.write(raw) != len(raw): raise OSError('short validation proof write')
            stream.flush(); os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
