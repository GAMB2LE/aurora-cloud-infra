"""Journalled stream transport for one radar version, without a local spool.

This primitive is not an admission controller. The caller must enforce the
existing source/transfer locks, fresh source and archive absence checks, exact
command and script pins, independent readback and GWS/object reconciliation.
No path can be accepted for eviction or retention here.
"""
import datetime
import json
import os
import re
import resource
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from gws_receive import PATH, SHA, MAX_SIZE

MAX_DIAGNOSTIC = 1024 * 1024


def _bound_child_files():
    # The sender payload uses a pipe, not a regular file. Bound only files a
    # malfunctioning sender or receiver might write, including diagnostics.
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_DIAGNOSTIC, MAX_DIAGNOSTIC))


def _append(fd, value):
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    while raw:
        written = os.write(fd, raw)
        if written <= 0: raise OSError('short journal write')
        raw = raw[written:]
    os.fsync(fd)


def _json_file(f, *, last_line=False):
    f.flush(); size = f.tell()
    if size <= 0 or size > MAX_DIAGNOSTIC: raise ValueError('invalid diagnostic size')
    f.seek(0); raw = f.read()
    if last_line:
        raw = raw.rstrip().split(b'\n')[-1]
    value = json.loads(raw)
    if not isinstance(value, dict): raise ValueError('invalid transport receipt')
    return value


def _stop(process):
    if process is not None and process.poll() is None:
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait(timeout=3)


def transfer(source_command, destination_command, expected, journal, *, operation, timeout=7200):
    if not (isinstance(source_command, list) and source_command and all(isinstance(x, str) for x in source_command)
            and isinstance(destination_command, list) and destination_command and all(isinstance(x, str) for x in destination_command)
            and isinstance(expected, dict) and set(expected) == {'relative_path', 'size', 'mtime_ns', 'sha256'}
            and isinstance(expected['relative_path'], str) and PATH.fullmatch(expected['relative_path'])
            and type(expected['size']) is int and 0 < expected['size'] <= MAX_SIZE
            and type(expected['mtime_ns']) is int and 0 <= expected['mtime_ns'] < 2**63
            and isinstance(expected['sha256'], str) and SHA.fullmatch(expected['sha256'])
            and isinstance(operation, dict) and set(operation) == {'nonce', 'source_script_sha256', 'receiver_script_sha256', 'destination'}
            and isinstance(operation['nonce'], str) and re.fullmatch('[0-9a-f]{32}', operation['nonce'])
            and all(isinstance(operation[k], str) and SHA.fullmatch(operation[k]) for k in ('source_script_sha256', 'receiver_script_sha256'))
            and operation['destination'] in ('gws-canary', 'gws-canonical')
            and type(timeout) in (int, float) and 0 < timeout <= 7200):
        raise ValueError('invalid transfer request')
    journal = Path(journal)
    fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    source = destination = None
    try:
        parent = os.open(journal.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(parent)
        finally: os.close(parent)
        _append(fd, {'phase': 'intent', 'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     **expected, 'operation': operation, 'eviction_authorized': False, 'retention_authorized': False})
        with tempfile.TemporaryFile() as serr, tempfile.TemporaryFile() as dout, tempfile.TemporaryFile() as derr:
            try:
                source = subprocess.Popen(source_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=serr, start_new_session=True, preexec_fn=_bound_child_files)
                destination = subprocess.Popen(destination_command, stdin=source.stdout, stdout=dout,
                                               stderr=derr, start_new_session=True, preexec_fn=_bound_child_files)
                source.stdout.close()
                deadline = time.monotonic() + timeout
                while source.poll() is None or destination.poll() is None:
                    if time.monotonic() >= deadline: raise TimeoutError('stream deadline exceeded')
                    time.sleep(min(.05, max(0, deadline-time.monotonic())))
                if source.returncode != 0 or destination.returncode != 0:
                    raise ValueError('stream process failed')
                sent = _json_file(serr, last_line=True); received = _json_file(dout)
                if not (sent.get('schema') == 'radar-direct-source-stream-v1'
                        and sent.get('relative_path') == expected['relative_path']
                        and sent.get('read_bytes') == expected['size'] and sent.get('sha256') == expected['sha256']
                        and sent.get('source_stable') is True and sent.get('eviction_authorized') is False
                        and sent.get('retention_authorized') is False
                        and received.get('schema') == 'radar-direct-gws-receipt-v1'
                        and all(received.get(k) == v for k, v in expected.items())
                        and received.get('atomic_no_replace') is True
                        and received.get('source_stability_proven') is False
                        and received.get('independent_readback') is False
                        and received.get('immutable_backing_proven') is False
                        and received.get('eviction_authorized') is False
                        and received.get('retention_authorized') is False):
                    raise ValueError('transport receipts differ')
                result = {'phase': 'stream-complete', **expected, 'source_stable': True,
                          'independent_readback': False, 'immutable_backing_proven': False,
                          'eviction_authorized': False, 'retention_authorized': False}
                _append(fd, result)
                return result
            except BaseException:
                _stop(source); _stop(destination)
                # A receiver might have published before a failure or SSH
                # timeout. Never retry automatically or claim it is absent.
                _append(fd, {'phase': 'outcome-uncertain', **expected,
                             'source_exit': source.returncode if source else None,
                             'destination_exit': destination.returncode if destination else None,
                             'eviction_authorized': False, 'retention_authorized': False})
                raise
    finally:
        os.close(fd)
