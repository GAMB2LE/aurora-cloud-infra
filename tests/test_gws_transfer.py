"""Real rsync races use disposable trees only; no cloud credentials or archives."""
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).parents[1] / 'roles/gws_delivery/files/aurora-gws-transfer.py'
spec = importlib.util.spec_from_file_location('gws_transfer', SCRIPT)
gws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gws)
RSYNC = shutil.which('rsync')
REAL_RSYNC = bool(RSYNC and 'openrsync' not in subprocess.check_output([RSYNC, '--version'], text=True))
if os.environ.get('CI') and not REAL_RSYNC:
    raise RuntimeError('CI must run the upstream rsync race regressions')


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Fixture did not reach its expected boundary')
        time.sleep(.01)


class LockTests(unittest.TestCase):
    def test_discovery_retains_vanished_source_status(self):
        result = subprocess.CompletedProcess(
            ['rsync'], 24, 'AURORA\t>f+++++++++\t7\ta\t\n', 'source vanished\n',
        )
        with mock.patch.object(gws.subprocess, 'run', return_value=result), mock.patch.object(gws.sys, 'stderr') as error:
            self.assertEqual(gws.discover(['rsync', '-aH'], '/source/', '/dest/'), ([('a', 7)], 24))
            error.write.assert_called_once_with('source vanished\n')

    def test_bulk_keeps_discovery_and_batch_vanished_source_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('source', 'locks'):
                (root / name).mkdir()
            (root / 'source/a').write_text('payload')
            args = type('Args', (), dict(
                source=str(root / 'source'), destination=str(root / 'dest'),
                ssh_command='ssh', copy_links=False, exclude=[], max_files=1,
                max_bytes=100, lock_root=str(root / 'locks'), wait_seconds=1,
            ))()
            for discovery_code, batch_code in ((24, 0), (0, 24)):
                with self.subTest(discovery=discovery_code, batch=batch_code), mock.patch.object(
                    gws, 'discover', return_value=([('a', 7)], discovery_code),
                ), mock.patch.object(gws, 'supervised_run', return_value=batch_code):
                    self.assertEqual(gws.bulk(args), 24)

    def test_batch_sizes_are_refreshed_after_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            for name in ('a', 'b'):
                (source / name).write_bytes(b'x')
            pending = gws.batches([('a', 1), ('b', 1)], source, 2, 10)
            (source / 'a').write_bytes(b'x' * 8)
            (source / 'b').write_bytes(b'x' * 8)
            self.assertEqual(next(pending), [('a', 8)])
            (source / 'b').write_bytes(b'x' * 12)
            self.assertEqual(next(pending), [('b', 12)])

    def test_busy_bulk_does_not_hold_fast_intent(self):
        with tempfile.TemporaryDirectory() as root:
            with gws.delivery_lock(root, 'fast', .1):
                with self.assertRaises(gws.DeliveryBusy):
                    with gws.delivery_lock(root, 'bulk', .05):
                        self.fail('Bulk passed a waiting/active fast lane')
            with gws.delivery_lock(root, 'fast', .1):
                pass

    def test_symlink_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path/'target').write_text('untouched')
            (path/'delivery.lock').symlink_to(path/'target')
            with self.assertRaises(OSError):
                with gws.delivery_lock(path, 'fast', .1):
                    pass
            self.assertEqual((path/'target').read_text(), 'untouched')

    def test_waiting_dispatch_goes_before_next_bulk_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); order = root/'order'
            def command(label):
                return [sys.executable, '-c', f"from pathlib import Path; p=Path({str(order)!r}); p.open('a').write({label!r}+'\\n')"]
            with gws.delivery_lock(root, 'bulk', .1):
                fast = subprocess.Popen([sys.executable, str(SCRIPT), 'fast', '--lock-root', str(root), '--wait-seconds', '5', '--', *command('fast')])
                def fast_waiting():
                    fd = gws.open_lock(root/'fast-waiting.lock')
                    try:
                        try:gws.fcntl.flock(fd, gws.fcntl.LOCK_EX | gws.fcntl.LOCK_NB)
                        except BlockingIOError:return True
                        return False
                    finally:os.close(fd)
                wait_for(fast_waiting)
                # No stale marker and no queue ahead of the recorded fast waiter.
                with self.assertRaises(gws.DeliveryBusy):
                    with gws.delivery_lock(root, 'bulk', .05):
                        pass
            self.assertEqual(fast.wait(timeout=5), 0)
            self.assertEqual(gws.run_batch(command('bulk'), root, 'bulk', 1), 0)
            self.assertEqual(order.read_text().splitlines(), ['fast', 'bulk'])

    def test_term_keeps_lock_until_child_and_grandchild_finish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); started=root/'started'; finish=root/'finish'; done=root/'done'
            child = root/'child.py'
            child.write_text("import subprocess,sys\nsubprocess.run([sys.executable,'-c'," + repr(
                f"from pathlib import Path; import time; Path({str(started)!r}).touch(); "
                f"exec(\"while not Path({str(finish)!r}).exists(): time.sleep(.01)\"); Path({str(done)!r}).touch()"
            ) + "],check=True)\n")
            process = subprocess.Popen([sys.executable,str(SCRIPT),'fast','--lock-root',str(root),'--',sys.executable,str(child)])
            try:
                wait_for(started.exists)
                process.send_signal(signal.SIGTERM)
                with self.assertRaises(gws.DeliveryBusy):
                    with gws.delivery_lock(root,'fast',.1):pass
                self.assertIsNone(process.poll())
                finish.touch()
                self.assertEqual(process.wait(timeout=5),143)
                self.assertTrue(done.exists())
                with gws.delivery_lock(root,'fast',.1):pass
            finally:
                finish.touch()
                process.wait(timeout=5)


@unittest.skipUnless(REAL_RSYNC, 'Requires upstream rsync, not macOS openrsync')
class RealRsyncTests(unittest.TestCase):
    def race(self, separate=False, update=False, locked=False):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';fast_source=root/'fast';dest=root/'dest';locks=root/'locks'
            for path in (source,fast_source,dest,locks):path.mkdir()
            old=b'old\n'*1024;new=b'new\n'*1024
            (source/'a.dat').write_bytes(old);(source/'z-padding').write_bytes(b'x'*(4*1024*1024))
            os.utime(source/'a.dat',(time.time()-120,)*2)
            (fast_source/'a.dat').write_bytes(new if separate or locked else old)
            os.utime(fast_source/'a.dat',(time.time()-60 if separate or locked else (source/'a.dat').stat().st_mtime,)*2)
            args=[RSYNC,'-a','--delay-updates','--bwlimit=1024']
            if separate:args += ['--partial-dir=.slow']
            if update:args += ['--update']
            command=args+[str(source)+'/',str(dest)+'/']
            if locked:command=[sys.executable,str(SCRIPT),'fast','--lock-root',str(locks),'--',*command]
            slow=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            fast=None
            try:
                wait_for((dest/('.slow' if separate else '.~tmp~')/'a.dat').exists)
                fast_args=[RSYNC,'-a','--delay-updates']
                if separate:fast_args+=['--partial-dir=.fast']
                if locked:
                    # Both supported lanes read the same current source. Change it
                    # after the slow transfer captured its old version.
                    (source/'a.dat').write_bytes(new)
                    os.utime(source/'a.dat',(time.time(),)*2)
                    fast_args=[sys.executable,str(SCRIPT),'fast','--lock-root',str(locks),'--',*fast_args]
                    fast_input=source/'a.dat'
                else:fast_input=str(fast_source)+'/'
                fast=subprocess.Popen(fast_args+[str(fast_input),str(dest)+'/'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                if not locked:
                    self.assertEqual(fast.wait(timeout=5),0)
                    if separate:self.assertEqual((dest/'a.dat').read_bytes(),new)
                out,err=slow.communicate(timeout=12)
                self.assertEqual(fast.wait(timeout=5),0)
                return slow.returncode,err,(dest/'a.dat').read_bytes()==new
            finally:
                for process in (slow,fast):
                    if process is not None:
                        if process.poll() is None:process.kill()
                        process.communicate()

    def test_reproduces_original_shared_staging_collision(self):
        code,error,_=self.race()
        self.assertEqual(code,23)
        self.assertIn('rename failed',error)

    def test_separate_staging_and_update_do_not_prevent_stale_overwrite(self):
        code,error,newest=self.race(separate=True,update=True)
        self.assertEqual(code,0,error)
        self.assertFalse(newest)

    def test_serialized_transfers_leave_newer_source_version(self):
        code,error,newest=self.race(locked=True)
        self.assertEqual(code,0,error)
        self.assertTrue(newest)

    def test_bulk_preserves_directories_symlinks_hardlinks_and_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';dest=root/'dest';locks=root/'locks'
            for path in (source,dest,locks):path.mkdir()
            (source/'sub').mkdir();(source/'empty').mkdir()
            (source/'sub/a').write_bytes(b'payload')
            os.link(source/'sub/a',source/'sub/b')
            (source/'pointer').symlink_to('sub/a')
            os.chmod(source/'sub/a',0o640)
            for path in [source/'sub/a',source/'sub',source/'empty',source]:os.utime(path,(1600000000,)*2)
            result=subprocess.run([sys.executable,str(SCRIPT),'bulk','--lock-root',str(locks),'--max-files','1','--max-bytes','1','--source',str(source),'--destination',str(dest),'--ssh-command','ssh'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            self.assertEqual((dest/'sub/a').read_bytes(),b'payload')
            self.assertEqual((dest/'sub/a').stat().st_ino,(dest/'sub/b').stat().st_ino)
            self.assertEqual(stat.S_IMODE((dest/'sub/a').stat().st_mode),0o640)
            self.assertEqual(os.readlink(dest/'pointer'),'sub/a')
            self.assertTrue((dest/'empty').is_dir())
            self.assertEqual(int((dest/'sub').stat().st_mtime),1600000000)
            self.assertIn('oversized file/hardlink group',result.stdout)

    def test_actual_batch_rechecks_source_after_old_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';dest=root/'dest';locks=root/'locks'
            for path in (source,dest,locks):path.mkdir()
            (source/'a').write_text('old')
            command=[RSYNC,'-aH','--partial','--delay-updates']
            names, code=gws.discover(command,str(source)+'/',str(dest)+'/')
            self.assertEqual(code, 0)
            self.assertIn('a',[name for name,_ in names])
            (source/'a').write_text('newer-current-source')
            self.assertEqual(gws.run_batch(command+[str(source)+'/',str(dest)+'/'],locks,'fast'),0)
            filelist=root/'files';filelist.write_bytes(b''.join(os.fsencode(name)+b'\0' for name,_ in names))
            self.assertEqual(gws.run_batch(command+['--from0','--files-from='+str(filelist),str(source)+'/',str(dest)+'/'],locks,'bulk'),0)
            self.assertEqual((dest/'a').read_text(),'newer-current-source')

    def test_bulk_retains_hardlink_to_unchanged_destination_peer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, dest, locks = (root / name for name in ('source', 'dest', 'locks'))
            for path in (source, dest, locks):
                path.mkdir()
            (source / 'a').write_bytes(b'payload')
            os.link(source / 'a', source / 'b')
            subprocess.run([RSYNC, '-a', str(source / 'a'), str(dest) + '/'], check=True)
            result = subprocess.run([
                sys.executable, str(SCRIPT), 'bulk', '--lock-root', str(locks),
                '--max-files', '1', '--max-bytes', '1', '--source', str(source),
                '--destination', str(dest), '--ssh-command', 'ssh',
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((dest / 'a').stat().st_ino, (dest / 'b').stat().st_ino)
            self.assertEqual((dest / 'a').read_bytes(), b'payload')

    def test_bulk_excludes_local_state_and_preserves_copy_links_option(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';dest=root/'dest';locks=root/'locks'
            for path in (source,dest,locks):path.mkdir()
            (source/'real').write_text('content')
            (source/'link').symlink_to('real')
            (source/'rpgfmcw94/.radar-partials').mkdir(parents=True)
            (source/'rpgfmcw94/.radar-partials/work').write_text('local-only')
            result=subprocess.run([sys.executable,str(SCRIPT),'bulk','--lock-root',str(locks),'--max-files','1','--max-bytes','1','--copy-links','--exclude=rpgfmcw94/.radar-partials/**','--source',str(source),'--destination',str(dest),'--ssh-command','ssh'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertFalse((dest/'link').is_symlink())
            self.assertEqual((dest/'link').read_text(),'content')
            self.assertFalse((dest/'rpgfmcw94/.radar-partials/work').exists())

    def test_encoded_filename_fails_discovery_before_transfer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';dest=root/'dest'
            source.mkdir();dest.mkdir();(source/'line\nbreak').write_text('preserved')
            with self.assertRaisesRegex(RuntimeError,'encoded'):
                gws.discover([RSYNC,'-aH'],str(source)+'/',str(dest)+'/')
            self.assertEqual(list(dest.iterdir()),[])

    def test_bulk_reports_transfer_23_without_claiming_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);root.joinpath('source').mkdir();root.joinpath('locks').mkdir()
            root.joinpath('source/a').write_text('payload')
            args=type('Args',(),dict(source=str(root/'source'),destination=str(root/'dest'),ssh_command='ssh',copy_links=False,exclude=[],max_files=1,max_bytes=100,lock_root=str(root/'locks'),wait_seconds=1))()
            with mock.patch.object(gws,'discover',return_value=([('a',7)],0)),mock.patch.object(gws,'supervised_run',return_value=23):
                self.assertEqual(gws.bulk(args),23)
            self.assertEqual((root/'source/a').read_text(),'payload')

    def test_interrupted_partial_retries_from_current_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';dest=root/'dest';locks=root/'locks'
            for path in (source,dest,locks):path.mkdir()
            (source/'a').write_bytes(b'old-source'*(512*1024))
            (dest/'a').write_bytes(b'previous-complete')
            command=[RSYNC,'-a','--partial','--delay-updates','--bwlimit=1024',str(source)+'/',str(dest)+'/']
            process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                wait_for(lambda:any(p.stat().st_size>0 for p in dest.glob('.a.*')))
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=8)
                self.assertNotEqual(process.returncode,0)
                self.assertEqual((dest/'a').read_bytes(),b'previous-complete')
                self.assertTrue((dest/'.~tmp~/a').exists())
                current=b'current-source'*(512*1024)
                (source/'a').write_bytes(current)
                result=gws.run_batch([RSYNC,'-a','--partial','--delay-updates',str(source)+'/',str(dest)+'/'],locks,'fast')
                self.assertEqual(result,0)
                self.assertEqual((dest/'a').read_bytes(),current)
                self.assertFalse((dest/'.~tmp~/a').exists())
            finally:
                if process.poll() is None:process.kill();process.communicate()


if __name__ == '__main__':unittest.main()
