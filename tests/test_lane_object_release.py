import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'roles/radar_archive_recovery/files/coordinator'))
import gws_object_release as release


def test_lane_release_is_attempt_bound(tmp_path, monkeypatch):
    monkeypatch.setenv('RADAR_OBJECT_GWS_RELEASE', 'vm01-lanes')
    selected = release.select()
    assert selected['id'] == 'gws-object-vm01-lanes'
    stamp = '20261114T020000Z'
    out = tmp_path / ('direct-object-gzip-preparation-' + stamp); out.mkdir()
    directory = release.ROOT + '/direct-object-gzip-supervisor-v2/input-' + stamp.lower()
    values = {'request': {'operation_id': '20261027-gzip-next-' + stamp.lower() + '-0'},
              'continuity': {'accepted': True}, 'observation': {'size': 1}}
    argv = ['/usr/bin/python3', selected['launcher']]
    for name, value in values.items():
        raw = (json.dumps(value, sort_keys=True) + '\n').encode()
        (out / (name + '.json')).write_bytes(raw)
        argv += ['--' + name, directory + '/' + name + '.json', '--' + name + '-sha256', hashlib.sha256(raw).hexdigest()]
    (out / 'launcher-args.json').write_text(json.dumps(argv))
    with pytest.raises(ValueError, match='release record missing'): release.resolve(out)
    release.record(out, selected, argv)
    assert release.resolve(out) == (selected, argv)
    (out/'observation.json').write_text('{}')
    with pytest.raises(ValueError, match='hash differs'): release.resolve(out)
