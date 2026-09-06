from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

FILES = Path(__file__).parents[1] / 'roles/object_store_mirror/files'
sys.path.insert(0, str(FILES))
from test_object_store_s3_recovery import FakeS3

SCRIPT = Path(__file__).parents[1] / 'scripts/probe_archive_resume.py'
SPEC = importlib.util.spec_from_file_location('resume_probe', SCRIPT)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ResumeProbeTests(unittest.TestCase):
    def test_two_processes_resume_after_abrupt_committed_page_exit(self):
        runner = '''
import importlib.util
import sys
from test_object_store_s3_recovery import FakeS3
script, catalog, root, phase = sys.argv[1:]
spec = importlib.util.spec_from_file_location("isolated_probe", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
base = "products/cl61/gamb2le_depolarisation_lidar_ceilometer_aurora.zarr/longitude/"
module._client = lambda config: FakeS3([(base + name, 10) for name in (".zarray", ".zattrs", "0")])
sys.argv = [script, "--catalog", catalog, "--state-root", root, phase]
raise SystemExit(module.main())
'''
        with tempfile.TemporaryDirectory(prefix='archive-resume-probe.') as temporary:
            root = Path(temporary)
            catalog = root / 'catalog.json'
            catalog.write_text(json.dumps({'bucket': 'test',
                'jobs': [{'name': 'products', 'destination': 'products'}],
                'recovery_free_reserve_bytes': 0}))
            env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(FILES), str(Path(__file__).parent))))
            command = [sys.executable, '-c', runner, str(SCRIPT), str(catalog), temporary]
            interrupted = subprocess.run([*command, 'interrupt'], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(interrupted.returncode, 75, interrupted.stderr)
            first = json.loads(interrupted.stdout)
            self.assertEqual(first['pages_completed'], 1)
            self.assertEqual(first['requests'], 1)
            resumed = subprocess.run([*command, 'resume'], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            second = json.loads(resumed.stdout)
            self.assertEqual(second['verification_id'], first['verification_id'])
            self.assertTrue(second['compatible'])
            self.assertTrue(second['started_with_saved_cursor'])
            self.assertEqual(second['requests'], 1)
            self.assertEqual(second['objects_observed'], 3)

    def test_probe_records_commit_before_exit_and_resumes_only_one_page(self):
        config = {'bucket': 'test', 'jobs': [{'name': 'products', 'destination': 'products'}],
                  'recovery_free_reserve_bytes': 0}
        base = 'products/cl61/gamb2le_depolarisation_lidar_ceilometer_aurora.zarr/longitude/'
        client = FakeS3([(base + name, 10) for name in ('.zarray', '.zattrs', '0')])
        with tempfile.TemporaryDirectory(prefix='archive-resume-probe.') as temporary:
            with mock.patch.object(probe, '_client', return_value=client), mock.patch.object(
                    probe.os, '_exit', side_effect=SystemExit(75)), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as exited:
                    probe.run_probe(config, temporary, 'interrupt')
            self.assertEqual(exited.exception.code, 75)
            interrupted = json.loads((Path(temporary) / 'interrupted.json').read_text())
            self.assertEqual(interrupted['objects_observed'], 2)
            self.assertEqual(len(client.calls), 1)
            client = FakeS3([(base + name, 10) for name in ('.zarray', '.zattrs', '0')])
            with mock.patch.object(probe, '_client', return_value=client):
                resumed = probe.run_probe(config, temporary, 'resume')
            self.assertTrue(resumed['compatible'])
            self.assertEqual(resumed['objects_observed'], 3)
            self.assertEqual(resumed['verification_id'], interrupted['verification_id'])
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(client.calls[0]['ContinuationToken'], '2')

    def test_resume_without_committed_interruption_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='archive-resume-probe.') as temporary:
            with self.assertRaises(probe.ProbeFailure):
                probe.run_probe({}, temporary, 'resume')


if __name__ == '__main__':
    unittest.main()
