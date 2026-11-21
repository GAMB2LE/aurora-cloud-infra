"""Validate the exact native LV1 version accepted by finalized delivery."""
import re


def valid_expected(value):
    return (isinstance(value, dict) and set(value) == {'relative_path', 'size', 'mtime_ns', 'sha256'}
            and isinstance(value['relative_path'], str)
            and re.fullmatch(r'Y\d{4}/M\d{2}/D\d{2}/[A-Za-z0-9_.-]+\.LV1', value['relative_path']) is not None
            and type(value['size']) is int and 0 < value['size'] <= 4 * 1024**3
            and type(value['mtime_ns']) is int and value['mtime_ns'] > 0
            and isinstance(value['sha256'], str) and re.fullmatch('[0-9a-f]{64}', value['sha256']) is not None)
