from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "roles/source_sync/templates/aurora-power-sync.j2"
SERVICE = ROOT / "roles/source_sync/templates/aurora-power-source-sync.service.j2"
TASKS = ROOT / "roles/source_sync/tasks/main.yml"


class PowerSourceSyncTests(unittest.TestCase):
    def test_ssh_and_rsync_are_bounded(self) -> None:
        sync = SYNC.read_text(encoding="utf-8")

        self.assertIn("ConnectionAttempts=1", sync)
        self.assertIn('ConnectTimeout="$connect_timeout_seconds"', sync)
        self.assertIn('ServerAliveInterval="$server_alive_interval_seconds"', sync)
        self.assertIn('ServerAliveCountMax="$server_alive_count_max"', sync)
        self.assertGreaterEqual(sync.count("ConnectTimeout"), 4)

    def test_systemd_caps_the_complete_sync(self) -> None:
        service = SERVICE.read_text(encoding="utf-8")

        self.assertIn(
            "TimeoutStartSec={{ power_source_service_timeout_seconds | int }}",
            service,
        )

    def test_power_sync_can_be_deployed_without_unrelated_sources(self) -> None:
        tasks = TASKS.read_text(encoding="utf-8")
        power_block = tasks.split("- name: Install power source sync script", 1)[1]
        power_block = power_block.split("- name: Install PDU source sync script", 1)[0]

        self.assertEqual(power_block.count("tags:\n    - power_source_sync"), 4)


if __name__ == "__main__":
    unittest.main()
