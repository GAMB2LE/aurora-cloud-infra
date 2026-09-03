import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
RECONCILE = ROOT / "roles/source_sync/templates/aurora-wxcam-reconcile.j2"
RECONCILE_SERVICE = (
    ROOT / "roles/source_sync/templates/aurora-wxcam-reconcile.service.j2"
)
RECONCILE_TIMER = (
    ROOT / "roles/source_sync/templates/aurora-wxcam-reconcile.timer.j2"
)
LIVE_SYNC = ROOT / "roles/source_sync/templates/aurora-wxcam-sync.j2"
TASKS = ROOT / "roles/source_sync/tasks/main.yml"
MONITOR_TASKS = ROOT / "roles/operations_monitor/tasks/main.yml"
GROUP_VARS = ROOT / "inventory/group_vars/aurora_cloud.yml"


class WxcamSourceSyncTests(unittest.TestCase):
    @staticmethod
    def _shell_function(source: str, name: str) -> str:
        match = re.search(rf"^{name}\(\) \{{.*?^\}}$", source, re.M | re.S)
        if match is None:
            raise AssertionError(f"missing shell function: {name}")
        return match.group(0)

    def test_recent_reconciliation_does_not_trust_the_live_checkpoint(self) -> None:
        source = RECONCILE.read_text(encoding="utf-8")
        remote_find = source.split("<<'REMOTE_FIND'", 1)[1].split(
            "\nREMOTE_FIND", 1
        )[0]

        self.assertIn("offset = 1; offset <= lookback_days", remote_find)
        self.assertIn('date -u -d "-${offset} day" +%Y%m%d', remote_find)
        self.assertNotIn("scan_epoch", remote_find)
        self.assertNotIn("last_epoch", remote_find)
        self.assertIn('! -newermt "@${upper_epoch}"', remote_find)
        self.assertIn('! -newerct "@${upper_epoch}"', remote_find)

    def test_reconciliation_is_copy_only_and_enqueues_only_copied_paths(self) -> None:
        source = RECONCILE.read_text(encoding="utf-8")

        self.assertIn("rsync -a --ignore-existing --omit-dir-times", source)
        self.assertIn("--no-perms --no-owner --no-group", source)
        self.assertNotIn("--partial", source)
        self.assertNotIn("--delete", source)
        self.assertNotIn("--remove-source-files", source)
        self.assertNotIn("rm -rf", source)
        self.assertNotIn(" -delete", source)
        self.assertIn("--itemize-changes", source)
        # %b is a transfer-statistic escape, so rsync emits the receipt only
        # after the file transfer finishes rather than before it starts.
        self.assertIn("--out-format=$'%i%b\\t%n'", source)
        self.assertIn('--files-from "$pending_file"', source)
        self.assertIn("--records-format rsync", source)
        self.assertIn("--job raw", source)

    def test_reconciliation_serializes_destination_writes_with_live_sync(self) -> None:
        reconcile = RECONCILE.read_text(encoding="utf-8")
        live = LIVE_SYNC.read_text(encoding="utf-8")
        expected_live_lock = (
            "lock_file={{ aurora_state_root | quote }}/wxcam-sync.lock"
        )
        expected_reconcile_lock = (
            "destination_lock_file={{ aurora_state_root | quote }}/wxcam-sync.lock"
        )

        self.assertIn(expected_reconcile_lock, reconcile)
        self.assertIn(expected_live_lock, live)
        self.assertIn("run_lock_file={{ aurora_state_root | quote }}", reconcile)
        self.assertIn("if ! flock -n 8; then", reconcile)
        lock = reconcile.index('if ! flock -w 120 9; then')
        destination_create = reconcile.index('mkdir -p "$destination"')
        transfer = reconcile.index("rsync -a --ignore-existing")
        unlock = reconcile.index("flock -u 9")
        post_transfer_replay = reconcile.index(
            "flush_pending_receipts || receipt_status=$?"
        )
        self.assertLess(lock, destination_create)
        self.assertLess(destination_create, transfer)
        self.assertLess(lock, transfer)
        self.assertLess(transfer, unlock)
        self.assertLess(unlock, post_transfer_replay)

    def test_receipt_capture_persists_only_completed_file_additions(self) -> None:
        source = RECONCILE.read_text(encoding="utf-8")
        function = self._shell_function(source, "record_additions")
        records = (
            ">f+++++++++1048576\tPANO/20260902/HDR_a_PANO.jpg\n"
            ".d..t......0\tPANO/20260902/\n"
            ">f+++++++++2097152\tFISH/20260902/HDR_b.mp4\n"
        )

        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary) / "pending"
            command = (
                f"pending_file={shlex.quote(str(pending))}\n"
                f"{function}\n"
                "record_additions\n"
            )
            result = subprocess.run(
                ["bash", "-c", command],
                input=records,
                text=True,
                capture_output=True,
                check=True,
            )

            persisted = pending.read_text(encoding="utf-8")

        self.assertEqual(result.stdout, records)
        self.assertEqual(
            persisted,
            ">f+++++++++1048576\tPANO/20260902/HDR_a_PANO.jpg\n"
            ">f+++++++++2097152\tFISH/20260902/HDR_b.mp4\n",
        )

    def test_pending_receipts_are_deduplicated_and_removed_after_enqueue(self) -> None:
        source = RECONCILE.read_text(encoding="utf-8")
        function = self._shell_function(source, "flush_pending_receipts")
        record = ">f+++++++++1048576\tPANO/20260902/HDR_a_PANO.jpg\n"
        duplicate_path = ">f+++++++++2097152\tPANO/20260902/HDR_a_PANO.jpg\n"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root / "pending"
            captured = root / "captured"
            dispatcher = root / "dispatch"
            pending.write_text(record + duplicate_path, encoding="utf-8")
            dispatcher.write_text(
                '#!/bin/sh\ncp "$7" "$CAPTURE_PATH"\nexit "${DISPATCH_EXIT:-0}"\n',
                encoding="utf-8",
            )
            dispatcher.chmod(0o755)
            command = (
                "set -euo pipefail\n"
                f"pending_file={shlex.quote(str(pending))}\n"
                f"archive_dispatch={shlex.quote(str(dispatcher))}\n"
                "destination=/archive/raw/wxcam\n"
                f"{function}\n"
                "flush_pending_receipts\n"
            )
            environment = os.environ.copy()
            environment["CAPTURE_PATH"] = str(captured)
            subprocess.run(
                ["bash", "-c", command],
                env=environment,
                text=True,
                capture_output=True,
                check=True,
            )

            self.assertEqual(captured.read_text(encoding="utf-8"), record)
            self.assertFalse(pending.exists())

    def test_pending_receipts_survive_dispatch_failure_for_replay(self) -> None:
        source = RECONCILE.read_text(encoding="utf-8")
        function = self._shell_function(source, "flush_pending_receipts")
        record = ">f+++++++++1048576\tPANO/20260902/HDR_a_PANO.jpg\n"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root / "pending"
            dispatcher = root / "dispatch"
            pending.write_text(record, encoding="utf-8")
            dispatcher.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
            dispatcher.chmod(0o755)
            command = (
                "set -euo pipefail\n"
                f"pending_file={shlex.quote(str(pending))}\n"
                f"archive_dispatch={shlex.quote(str(dispatcher))}\n"
                "destination=/archive/raw/wxcam\n"
                f"{function}\n"
                "flush_pending_receipts\n"
            )
            result = subprocess.run(
                ["bash", "-c", command],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(pending.read_text(encoding="utf-8"), record)

    def test_reconciliation_keeps_the_exact_commissioned_media_scope(self) -> None:
        source = RECONCILE.read_text(encoding="utf-8")
        group_vars = GROUP_VARS.read_text(encoding="utf-8")

        self.assertIn('wxcam_source_fish_pattern: "HDR_*.jpg"', group_vars)
        self.assertIn('wxcam_source_pano_pattern: "HDR_*_PANO.jpg"', group_vars)
        self.assertIn(
            "fish_jpg_pattern={{ wxcam_source_fish_pattern | quote }}", source
        )
        self.assertIn(
            "pano_jpg_pattern={{ wxcam_source_pano_pattern | quote }}", source
        )
        self.assertIn('fish_mp4_pattern="${fish_jpg_pattern%.jpg}.mp4"', source)
        self.assertIn('pano_mp4_pattern="${pano_jpg_pattern%.jpg}.mp4"', source)

    def test_reconciliation_is_installed_as_an_independent_timer(self) -> None:
        tasks = TASKS.read_text(encoding="utf-8")
        monitor_tasks = MONITOR_TASKS.read_text(encoding="utf-8")
        service = RECONCILE_SERVICE.read_text(encoding="utf-8")
        timer = RECONCILE_TIMER.read_text(encoding="utf-8")

        self.assertIn("Install WXcam recent archive reconciliation script", tasks)
        self.assertIn("aurora-wxcam-reconcile.service", tasks)
        self.assertIn("aurora-wxcam-reconcile.timer", tasks)
        self.assertIn("wxcam_reconcile_timer_enabled | bool", tasks)
        self.assertGreaterEqual(tasks.count("- wxcam_reconcile"), 4)
        self.assertIn("TimeoutStartSec=2h", service)
        self.assertIn("Nice=15", service)
        self.assertIn("IOSchedulingPriority=7", service)
        self.assertIn("OnUnitActiveSec={{ wxcam_reconcile_interval }}", timer)
        self.assertIn("Persistent=true", timer)
        archive_health_task = monitor_tasks.split(
            "- name: Install archive health collector", 1
        )[1].split("- name:", 1)[0]
        self.assertIn("- wxcam_reconcile", archive_health_task)

    def test_pending_receipts_are_replayed_before_new_transfer(self) -> None:
        source = RECONCILE.read_text(encoding="utf-8")

        initial_replay = source.index("\nflush_pending_receipts\n")
        source_connection = source.index('\nif [[ "$source_auth" == "ssh_key" ]]')
        post_transfer_replay = source.index(
            "\nflush_pending_receipts || receipt_status=$?"
        )
        transfer = source.index("\nrsync -a --ignore-existing")

        self.assertLess(initial_replay, source_connection)
        self.assertGreater(post_transfer_replay, transfer)

    def test_production_policy_bounds_and_monitors_reconciliation(self) -> None:
        source = GROUP_VARS.read_text(encoding="utf-8")

        self.assertIn("wxcam_reconcile_lookback_days: 10", source)
        self.assertIn("wxcam_reconcile_interval: 6h", source)
        wxcam_stream = source.split("  - name: wxcam", 1)[1].split(
            "  - name: auroracam", 1
        )[0]
        self.assertIn("source_sync_auxiliary_stems:", wxcam_stream)
        self.assertIn("- wxcam-reconcile", wxcam_stream)

        lookback = re.search(r"^wxcam_reconcile_lookback_days: (\d+)$", source, re.M)
        retention = re.search(r"^    retention_days: (\d+)$", wxcam_stream, re.M)
        self.assertIsNotNone(lookback)
        self.assertIsNotNone(retention)
        self.assertGreater(int(lookback.group(1)), int(retention.group(1)))


if __name__ == "__main__":
    unittest.main()
