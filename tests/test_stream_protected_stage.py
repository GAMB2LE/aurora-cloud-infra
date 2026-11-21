import gzip
import io
import os

import pytest

from test_stage_native_version import fixture
from stage_native_version import stage
from stream_protected_stage import stream, stream_version


def test_streams_exact_staged_bytes_and_refuses_other_versions(tmp_path):
    root, destination, source, eligibility, captures, now = fixture(tmp_path)
    proof = stage(root, destination, eligibility, 'c' * 32, captures=captures, minimum_free_bytes=0, now=now+1)
    expected = {'relative_path': proof['source'], 'size': proof['size'], 'sha256': proof['sha256']}
    output = io.BytesIO()
    receipt = stream(destination, 'c' * 32, expected, output, owner=os.getuid())
    assert gzip.decompress(output.getvalue()) == source.read_bytes()
    assert receipt['read_bytes'] == expected['size']
    assert receipt['retention_authorized'] is receipt['eviction_authorized'] is False
    group_reader_output = io.BytesIO()
    stream_version(destination, 'c' * 32, expected, proof['stage_identity'], group_reader_output, owner=os.getuid())
    assert gzip.decompress(group_reader_output.getvalue()) == source.read_bytes()
    with pytest.raises(ValueError, match='identity'):
        stream_version(destination, 'c' * 32, expected, [0, *proof['stage_identity'][1:]], io.BytesIO(), owner=os.getuid())
    with pytest.raises(ValueError, match='differs'):
        stream(destination, 'c' * 32, {**expected, 'sha256': 'f' * 64}, io.BytesIO(), owner=os.getuid())
    os.chmod(proof['stage_path'], 0o640)
    with pytest.raises(ValueError, match='protected'):
        stream(destination, 'c' * 32, expected, io.BytesIO(), owner=os.getuid())
