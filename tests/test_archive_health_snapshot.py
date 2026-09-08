"""Publication contention is a deferred health read, not missing evidence."""
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


TEMPLATE = Path(__file__).parents[1] / "roles/operations_monitor/templates/aurora-archive-health.py.j2"


def reader_namespace(root):
    source = TEMPLATE.read_text()
    namespace = {
        "Path": Path, "RECOVERY_ENABLED": True,
        "OBJECT": root / "latest/comparison.json", "GATE": root / "gate.json",
        "GWS": root / "gws.json", "OUTPUT": root / "health.json",
        "sys": SimpleNamespace(path=mock.Mock(), stderr=io.StringIO()),
        "time": SimpleNamespace(sleep=mock.Mock()), "read": mock.Mock(return_value={}),
    }
    snapshot_code = "def archive_snapshot" + source.split("def archive_snapshot", 1)[1].split("\n\ndef unit", 1)[0]
    main_code = "def main" + source.split("def main", 1)[1].split('\n\nif __name__', 1)[0]
    exec(snapshot_code + "\n\n" + main_code, namespace)
    return namespace


class ArchiveHealthSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.reader = mock.Mock(side_effect=BlockingIOError("publication is busy"))
        patcher = mock.patch.dict(sys.modules, {
            "aurora_object_store_evidence": SimpleNamespace(read_snapshot=self.reader),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.namespace = reader_namespace(self.root)

    def test_exhausted_contention_is_not_an_empty_inventory(self):
        with self.assertRaises(BlockingIOError):
            self.namespace["archive_snapshot"]()
        self.assertEqual(self.reader.call_count, 10)
        self.assertEqual(self.namespace["time"].sleep.call_count, 10)
        self.namespace["read"].assert_not_called()

    def test_deferred_update_preserves_exact_previous_health_and_timestamp(self):
        output = self.namespace["OUTPUT"]
        previous = b'{"generated_at":"2026-09-08T06:41:01Z","overall_level":"green"}\n'
        output.write_bytes(previous)
        before = output.stat()
        self.assertEqual(self.namespace["main"](), 0)
        self.assertEqual(output.read_bytes(), previous)
        self.assertEqual(output.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(output.with_suffix(".tmp").exists())
        self.namespace["read"].assert_not_called()
        self.assertIn("deferred", self.namespace["sys"].stderr.getvalue())

    def test_deferred_first_update_does_not_invent_a_health_record(self):
        self.assertEqual(self.namespace["main"](), 0)
        self.assertFalse(self.namespace["OUTPUT"].exists())

    def test_real_publication_lock_defers_then_next_read_uses_bound_evidence(self):
        script = TEMPLATE.parents[3] / "roles/object_store_mirror/files/aurora_object_store_evidence.py"
        spec = importlib.util.spec_from_file_location("health_evidence_test", script)
        evidence = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(evidence)
        self.reader.side_effect = evidence.read_snapshot
        generation = self.root / "generations/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        generation.mkdir(parents=True)
        (self.root / "latest").symlink_to(generation, target_is_directory=True)
        (generation / ".pin.lock").touch()
        report = {"generated_at": "2026-09-08T06:40:22Z", "jobs": {"raw": {"verification_id": "unchanged"}}}
        data = json.dumps(report).encode()
        gate = {"last_generated_at": report["generated_at"], "report_sha256": hashlib.sha256(data).hexdigest()}
        (generation / "comparison.json").write_bytes(data)
        (generation / "verification-gate.json").write_text(json.dumps(gate))
        with (self.root / ".inventory.lock").open("wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.namespace["main"](), 0)
            self.assertFalse(self.namespace["OUTPUT"].exists())
        self.assertEqual(self.namespace["archive_snapshot"](), (report, gate))
        self.assertEqual((generation / "comparison.json").read_bytes(), data)

    def test_short_contention_returns_one_pinned_report_and_gate(self):
        report, gate = {"generated_at": "original"}, {"report_sha256": "bound"}
        closed = []

        @contextmanager
        def snapshot():
            try:
                yield SimpleNamespace(report=report, gate=gate)
            finally:
                closed.append(True)

        self.reader.side_effect = [BlockingIOError("busy"), snapshot()]
        self.assertEqual(self.namespace["archive_snapshot"](), (report, gate))
        self.assertEqual(closed, [True])
        self.namespace["time"].sleep.assert_called_once_with(0.2)

    def test_missing_corrupt_and_unreadable_evidence_still_fail_closed(self):
        for error in (FileNotFoundError("missing"), ValueError("binding mismatch"), PermissionError("denied")):
            with self.subTest(error=type(error).__name__):
                self.reader.reset_mock(side_effect=True)
                self.reader.side_effect = error
                self.assertEqual(self.namespace["archive_snapshot"](), ({}, {}))
                self.reader.assert_called_once()


if __name__ == "__main__":
    unittest.main()
