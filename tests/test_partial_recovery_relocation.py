"""Exercise preservation boundaries with tiny disposable recovery trees."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'recovery_move', Path(__file__).parents[1] / 'scripts/relocate_partial_recovery_20261024.py')
move = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(move)


@unittest.skipUnless(hasattr(os, 'listxattr'), 'Linux recovery helper requires xattr support')
class RecoveryRelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'original'
        (self.source / 'nested').mkdir(parents=True)
        (self.source / 'nested' / 'saved.zst').write_bytes(b'preserved compressed bytes')
        (self.source / 'manifest.jsonl').write_text('original recovery evidence\n')
        self.target = self.root / 'system' / 'recovery'
        self.receipt = self.target.with_suffix('.json')
        self.baseline = move.inventory(self.source, hashes=True)

    def run_move(self, check_users=lambda *roots: None):
        return move.relocate(self.source, self.target, self.receipt, self.baseline, check_users)

    def test_complete_copy_keeps_logical_paths_bytes_metadata_and_receipt(self):
        result = self.run_move()
        self.assertEqual(result['phase'], 'complete')
        self.assertTrue(self.source.is_symlink())
        self.assertEqual((self.source / 'nested' / 'saved.zst').read_bytes(), b'preserved compressed bytes')
        move.verify_copy(self.target, self.baseline)
        self.assertFalse(self.source.with_name('original.relocated-original').exists())
        self.assertEqual(len(json.loads(self.receipt.read_text())['removed_duplicates']), 2)

    def test_existing_destination_is_never_overwritten(self):
        self.target.mkdir(parents=True)
        sentinel = self.target / 'sentinel'
        sentinel.write_text('keep me')
        with self.assertRaisesRegex(RuntimeError, 'Refusing existing path'):
            self.run_move()
        self.assertEqual(sentinel.read_text(), 'keep me')
        self.assertFalse(self.source.is_symlink())

    def test_source_changed_after_copy_preserves_original_and_staging(self):
        verify_copy = move.verify_copy
        def changed_after_verification(root, baseline):
            result = verify_copy(root, baseline)
            (self.source / 'manifest.jsonl').write_text('changed recovery evidence\n')
            return result
        with patch.object(move, 'verify_copy', side_effect=changed_after_verification):
            with self.assertRaisesRegex(RuntimeError, 'Recovery identity changed'):
                self.run_move()
        self.assertFalse(self.source.is_symlink())
        self.assertTrue(self.target.with_name('recovery.copying').exists())
        self.assertFalse(self.target.exists())

    def test_corrupt_copy_prevents_path_switch_and_duplicate_release(self):
        original_copy = move.shutil.copy2
        def corrupting_copy(source, target):
            result = original_copy(source, target)
            Path(target).write_bytes(b'corruption')
            return result
        with patch.object(move.shutil, 'copy2', side_effect=corrupting_copy):
            with self.assertRaisesRegex(RuntimeError, 'Copied (metadata|bytes) differ'):
                self.run_move()
        self.assertFalse(self.source.is_symlink())
        self.assertEqual((self.source / 'nested' / 'saved.zst').read_bytes(), b'preserved compressed bytes')

    def test_active_consumer_after_switch_keeps_complete_backup(self):
        def busy_after_switch(*roots):
            if self.source.is_symlink():
                raise RuntimeError('Recovery has an open descriptor')
        with self.assertRaisesRegex(RuntimeError, 'open descriptor'):
            self.run_move(busy_after_switch)
        self.assertTrue(self.source.is_symlink())
        backup = self.source.with_name('original.relocated-original')
        move.verify_copy(backup, self.baseline)
        move.verify_copy(self.target, self.baseline)
        self.assertEqual(json.loads(self.receipt.read_text())['removed_duplicates'], [])

    def test_symlinks_and_hardlinks_are_rejected(self):
        link = self.source / 'unexpected'
        link.symlink_to(self.source / 'manifest.jsonl')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected file type'):
            move.inventory(self.source)
        link.unlink()
        os.link(self.source / 'manifest.jsonl', link)
        with self.assertRaisesRegex(RuntimeError, 'Hard-linked file'):
            move.inventory(self.source)

    def test_process_working_directory_blocks_move(self):
        proc = self.root / 'proc' / '123'
        (proc / 'fd').mkdir(parents=True)
        (proc / 'maps').write_text('')
        (proc / 'cwd').symlink_to(self.source / 'nested')
        with self.assertRaisesRegex(RuntimeError, 'cwd of PID 123'):
            move.no_open_users(self.source, proc_root=proc.parent)

    def test_failed_symlink_keeps_complete_target_and_original_backup(self):
        with patch.object(Path, 'symlink_to', side_effect=OSError('injected symlink failure')):
            with self.assertRaisesRegex(OSError, 'injected symlink failure'):
                self.run_move()
        self.assertFalse(os.path.lexists(self.source))
        move.verify_copy(self.source.with_name('original.relocated-original'), self.baseline)
        move.verify_copy(self.target, self.baseline)
        self.assertEqual(json.loads(self.receipt.read_text())['removed_duplicates'], [])

    def test_failed_release_intent_does_not_remove_duplicate(self):
        write_receipt = move.write_receipt
        def fail_before_release(path, record):
            if 'release_intent' in record:
                raise OSError('injected receipt failure')
            return write_receipt(path, record)
        with patch.object(move, 'write_receipt', side_effect=fail_before_release):
            with self.assertRaisesRegex(OSError, 'injected receipt failure'):
                self.run_move()
        move.verify_copy(self.source.with_name('original.relocated-original'), self.baseline)
        move.verify_copy(self.target, self.baseline)
        self.assertEqual(json.loads(self.receipt.read_text())['removed_duplicates'], [])


if __name__ == '__main__':
    unittest.main()
