from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = (
    Path(__file__).parents[1]
    / "roles"
    / "object_store_mirror"
    / "files"
    / "aurora-object-store-trigger-retention.py"
)
SPEC = importlib.util.spec_from_file_location("object_store_trigger", SCRIPT)
assert SPEC and SPEC.loader
trigger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trigger)


class ObjectStoreTriggerRetentionTests(unittest.TestCase):
    def test_unstable_verification_does_not_start_retention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(
                json.dumps({"clean": True, "stable_parity": False}),
                encoding="utf-8",
            )
            run = mock.Mock()

            result = trigger.trigger(state, "retention.service", run=run)

        self.assertEqual(result, 0)
        run.assert_not_called()

    def test_stable_verification_starts_retention_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(
                json.dumps({"clean": True, "stable_parity": True}),
                encoding="utf-8",
            )
            run = mock.Mock(return_value=subprocess.CompletedProcess([], 0))

            result = trigger.trigger(state, "retention.service", run=run)

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            ["/bin/systemctl", "--no-block", "start", "retention.service"],
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
