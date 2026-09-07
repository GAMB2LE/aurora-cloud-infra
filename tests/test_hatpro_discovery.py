import fcntl
import fnmatch
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "roles/source_sync/files/aurora_hatpro_discovery.py"
SPEC = importlib.util.spec_from_file_location("hatpro_discovery", SOURCE)
discovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discovery)
PATTERN = "HATPROG5-AURORA-ICELAND_*"
FILE = "HATPROG5-AURORA-ICELAND_260907_140208.LWP.NC"
DATED = "Y2026/M09/D07/" + FILE


class HatproDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.destination = self.root / "cloud"
        self.destination.mkdir()
        self.state = self.root / "state" / "hatpro-sync.last"
        self.state.parent.mkdir()
        self.state.write_text("200\n")
        self.dispatcher = self.root / "dispatcher"
        self.dispatcher.write_text("#!/bin/sh\nexit 0\n")
        self.dispatcher.chmod(0o700)
        self.config = SimpleNamespace(
            destination=str(self.destination), state_file=str(self.state),
            source_user="aurora", source_host="source.invalid", source_port=22,
            source_path=str(self.source), source_pattern=PATTERN, source_auth="tailscale",
            ssh_key=str(self.root / "key"), known_hosts=str(self.root / "known_hosts"),
            start_fresh=False, dispatcher=str(self.dispatcher))
        self.calls, self.enqueued = [], []
        self.fail = None
        self.response = None

    @property
    def pending(self):
        return Path(str(self.state) + ".pending.json")

    def file(self, name=FILE, data=b"abcd", mtime=100):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (mtime, mtime))
        return path

    def mirror(self, name):
        target = self.destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.source / name, target)

    def inventory(self):
        result = bytearray(discovery.HEADER)
        for path in sorted(self.source.rglob("*")):
            if path.is_file() and fnmatch.fnmatchcase(path.name, PATTERN):
                info = path.stat()
                result += os.fsencode(path.relative_to(self.source)) + b"\0"
                result += str(info.st_size).encode() + b"\0"
                result += str(info.st_mtime).encode() + b"\0"
        return bytes(result) + discovery.TRAILER

    def execute(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == "ssh":
            if self.fail == "ssh":
                raise subprocess.CalledProcessError(255, args)
            return SimpleNamespace(stdout=self.response if self.response is not None else self.inventory())
        if args[0] == "rsync":
            names = Path(next(x.split("=", 1)[1] for x in args if x.startswith("--files-from="))).read_bytes()
            if self.fail == "rsync":
                raise subprocess.CalledProcessError(23, args)
            for name in names.rstrip(b"\0").split(b"\0"):
                self.mirror(os.fsdecode(name))
            return SimpleNamespace(returncode=0)
        if args[0] == str(self.dispatcher):
            if self.fail == "enqueue":
                raise subprocess.CalledProcessError(1, args)
            self.enqueued += [os.fsdecode(x) for x in Path(args[-2]).read_bytes().rstrip(b"\0").split(b"\0")]
            return SimpleNamespace(returncode=0)
        raise AssertionError("Unexpected external command")

    def run_sync(self, now=300):
        discovery.run(self.config, execute=self.execute, clock=lambda: now)

    def test_old_missing_dated_path_is_copied_despite_flat_duplicate_and_cursor(self):
        self.file()
        self.mirror(FILE)
        self.file(DATED)
        self.run_sync()
        self.assertEqual(self.enqueued, [DATED])
        self.assertEqual((self.destination / DATED).read_bytes(), b"abcd")
        self.assertEqual(self.state.read_text(), "300\n")

    def test_whole_directory_rename_is_found_without_changing_child_mtime(self):
        self.file("staging/" + FILE)
        self.run_sync()
        before = (self.source / "staging" / FILE).stat().st_mtime_ns
        (self.source / "Y2026/M09").mkdir(parents=True)
        (self.source / "staging").rename(self.source / "Y2026/M09/D07")
        self.run_sync(400)
        self.assertEqual((self.source / DATED).stat().st_mtime_ns, before)
        self.assertIn(DATED, self.enqueued)

    def test_same_path_size_or_mtime_change_behind_cursor_is_selected(self):
        self.file(DATED)
        self.mirror(DATED)
        self.file(DATED, data=b"larger", mtime=100)
        self.run_sync()
        self.assertEqual(self.enqueued, [DATED])
        self.file(DATED, data=b"larger", mtime=150)
        self.run_sync(400)
        self.assertEqual(self.enqueued, [DATED, DATED])

    def test_unchanged_tree_is_idempotent(self):
        self.file(DATED)
        self.mirror(DATED)
        self.run_sync()
        self.assertEqual(self.enqueued, [])
        self.assertEqual([call[0] for call in self.calls], ["ssh"])
        self.assertFalse(self.pending.exists())

    def test_future_source_timestamp_waits_then_is_discovered(self):
        self.file(DATED, mtime=350)
        self.run_sync()
        self.assertEqual(self.enqueued, [])
        self.run_sync(400)
        self.assertEqual(self.enqueued, [DATED])

    def test_empty_valid_inventory_advances_without_copy(self):
        self.run_sync()
        self.assertEqual(self.state.read_text(), "300\n")
        self.assertEqual(self.enqueued, [])

    def test_truncated_or_failed_listing_never_advances_cursor(self):
        for response in (b"", discovery.HEADER, discovery.HEADER + b"bad\0" + discovery.TRAILER):
            with self.subTest(response=response):
                self.response = response
                with self.assertRaises(ValueError):
                    self.run_sync()
                self.assertEqual(self.state.read_text(), "200\n")
        self.response = None
        self.fail = "ssh"
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_sync()
        self.assertEqual(self.state.read_text(), "200\n")

    def test_failed_transfer_retains_pending_for_next_process(self):
        self.file(DATED)
        self.fail = "rsync"
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_sync()
        self.assertIn(DATED, json.loads(self.pending.read_text())["files"])
        self.assertEqual(self.state.read_text(), "200\n")
        self.fail = None
        self.run_sync(400)
        self.assertEqual(self.enqueued, [DATED])
        self.assertFalse(self.pending.exists())

    def test_enqueue_failure_after_copy_replays_an_old_already_present_path(self):
        self.file(DATED)
        self.fail = "enqueue"
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_sync()
        self.assertTrue((self.destination / DATED).exists())
        self.assertTrue(self.pending.exists())
        self.assertEqual(self.state.read_text(), "200\n")
        self.fail = None
        self.run_sync(400)
        self.assertEqual(self.enqueued, [DATED])
        self.assertFalse(self.pending.exists())

    def test_disappearing_old_path_does_not_block_new_dated_path(self):
        self.file()
        self.fail = "rsync"
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_sync()
        (self.source / "Y2026/M09/D07").mkdir(parents=True)
        (self.source / FILE).rename(self.source / DATED)
        self.fail = None
        self.run_sync(400)
        self.assertEqual(self.enqueued, [DATED])
        self.assertFalse(self.pending.exists())

    def test_copied_receipt_survives_source_path_move(self):
        self.file()
        self.fail = "enqueue"
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_sync()
        (self.source / "Y2026/M09/D07").mkdir(parents=True)
        (self.source / FILE).rename(self.source / DATED)
        self.fail = None
        self.run_sync(400)
        self.assertEqual(set(self.enqueued), {FILE, DATED})

    def test_missing_dispatcher_preserves_pending_and_does_not_copy(self):
        self.file(DATED)
        self.dispatcher.unlink()
        with self.assertRaisesRegex(ValueError, "dispatcher unavailable"):
            self.run_sync()
        self.assertTrue(self.pending.exists())
        self.assertFalse((self.destination / DATED).exists())
        self.assertEqual(self.state.read_text(), "200\n")

    def test_corrupt_or_rebound_checkpoint_is_not_reset(self):
        self.file(DATED)
        self.fail = "rsync"
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_sync()
        old = self.pending.read_bytes()
        self.config.source_host = "different.invalid"
        with self.assertRaisesRegex(ValueError, "configuration mismatch"):
            self.run_sync()
        self.assertEqual(self.pending.read_bytes(), old)
        self.pending.write_text("not json")
        with self.assertRaises(ValueError):
            self.run_sync()
        self.assertEqual(self.pending.read_text(), "not json")

    def test_busy_backfill_defers_without_state_or_external_changes(self):
        self.file(DATED)
        with (self.state.parent / "hatpro-backfill.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_sync()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state.read_text(), "200\n")

    def test_symlink_destination_and_parent_are_rejected(self):
        self.file(DATED)
        (self.destination / "Y2026").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Unsafe destination"):
            self.run_sync()
        self.assertEqual(self.enqueued, [])

    def test_explicit_fresh_start_skips_history_but_finds_later_old_path(self):
        self.config.start_fresh = True
        self.state.unlink()
        self.file()
        self.run_sync()
        self.assertEqual(self.enqueued, [])
        self.file(DATED)
        self.run_sync(400)
        self.assertEqual(self.enqueued, [DATED])
        self.assertFalse((self.destination / FILE).exists())

    def test_absent_production_cursor_reconciles_history(self):
        self.state.unlink()
        self.file(DATED)
        self.run_sync()
        self.assertEqual(self.enqueued, [DATED])

    def test_rejects_path_escape_duplicates_and_invalid_metadata(self):
        for name in ("../" + FILE, "/" + FILE, "Y2026/../" + FILE, "unrelated.nc"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                discovery.parse_inventory(discovery.HEADER + name.encode() + b"\0" + b"4\0" + b"100\0" + discovery.TRAILER, PATTERN)
        for stamp in ("0", "-1", "NaN", "Infinity", "oops"):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                discovery.metadata(4, stamp)
        row = FILE.encode() + b"\0" + b"4\0" + b"100\0"
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            discovery.parse_inventory(discovery.HEADER + row + row + discovery.TRAILER, PATTERN)

    def test_space_and_newline_paths_remain_nul_delimited(self):
        name = "Y2026/M09/D07/" + FILE + " space\nname"
        self.file(name)
        self.run_sync()
        self.assertEqual(self.enqueued, [name])

    def test_copy_command_and_archive_target_are_preserved(self):
        self.file(DATED)
        self.run_sync()
        copy = next(x for x in self.calls if x[0] == "rsync")
        self.assertEqual(copy[:4], ["rsync", "-a", "--partial", "--from0"])
        self.assertFalse(any("--delete" in x or "--remove-source-files" in x for x in copy))
        enqueue = next(x for x in self.calls if x[0] == str(self.dispatcher))
        self.assertEqual(enqueue[1:4], ["enqueue", "--job", "raw"])
        self.assertEqual(enqueue[-1], "--null")
        self.assertTrue((self.source / DATED).exists())

    def test_invalid_cursor_or_clock_rollback_is_not_reset(self):
        self.state.write_text("broken\n")
        with self.assertRaisesRegex(ValueError, "Invalid HATPRO cursor"):
            self.run_sync()
        self.state.write_text("999\n")
        with self.assertRaisesRegex(ValueError, "Clock is behind"):
            self.run_sync()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
