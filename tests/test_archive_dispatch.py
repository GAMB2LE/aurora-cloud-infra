from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = (
    Path(__file__).parents[1]
    / "roles"
    / "archive_dispatch"
    / "files"
    / "aurora-archive-dispatch.py"
)
SPEC = importlib.util.spec_from_file_location("archive_dispatch", SCRIPT)
assert SPEC and SPEC.loader
dispatch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch)


class ArchiveDispatchTests(unittest.TestCase):
    def config(self, root: Path) -> dict:
        source = root / "raw"
        source.mkdir()
        return {
            "database": str(root / "state/queue.sqlite"),
            "status_path": str(root / "state/status.json"),
            "receipt_root": str(root / "state/receipts"),
            "lock_path": str(root / "state/worker.lock"),
            "rclone_config": str(root / "rclone.conf"),
            "object_remote": "object",
            "object_bucket": "bucket",
            "gws_user": "user",
            "gws_hosts": ["xfer.example"],
            "gws_key": "/key",
            "gws_known_hosts": "/known_hosts",
            "limits": {"max_files_per_run": 10, "max_bytes_per_run": 1_000_000},
            "jobs": {
                "raw": {
                    "source": str(source),
                    "gws_destination": "/gws/raw",
                    "object_destination": "data/raw",
                    "settle_age": "0s",
                    "exclude": ["**/*.partial"],
                }
            },
        }

    def test_enqueue_resets_both_destinations_when_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            source = Path(config["jobs"]["raw"]["source"])
            path = source / "hatprog5/file.nc"
            path.parent.mkdir()
            path.write_bytes(b"old")
            connection = dispatch.connect(config)
            try:
                dispatch.enqueue_paths(
                    config,
                    connection,
                    job_name="raw",
                    base=source,
                    paths=["hatprog5/file.nc"],
                )
                connection.execute(
                    "UPDATE delivery SET gws_delivered = 1, object_delivered = 1"
                )
                connection.commit()
                path.write_bytes(b"new-content")
                dispatch.enqueue_paths(
                    config,
                    connection,
                    job_name="raw",
                    base=source,
                    paths=["hatprog5/file.nc"],
                )
                row = connection.execute("SELECT * FROM delivery").fetchone()
            finally:
                connection.close()

        self.assertEqual(row["size"], len(b"new-content"))
        self.assertEqual(row["gws_delivered"], 0)
        self.assertEqual(row["object_delivered"], 0)

    def test_worker_delivers_newest_first_to_both_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            config["limits"]["max_files_per_run"] = 1
            source = Path(config["jobs"]["raw"]["source"])
            older = source / "cl61/older.nc"
            newer = source / "cl61/newer.nc"
            older.parent.mkdir()
            older.write_bytes(b"older")
            newer.write_bytes(b"newer")
            now = int(time.time()) - 60
            os.utime(older, (now - 60, now - 60))
            os.utime(newer, (now, now))
            connection = dispatch.connect(config)
            dispatch.enqueue_paths(
                config,
                connection,
                job_name="raw",
                base=source,
                paths=["cl61/older.nc", "cl61/newer.nc"],
            )
            delivered: list[tuple[str, list[str]]] = []

            def record_gws(_config, _job, rows, _path):
                delivered.append(("gws", [row["relative_path"] for row in rows]))

            def record_object(_config, _job, rows, _path):
                delivered.append(("object", [row["relative_path"] for row in rows]))

            try:
                with mock.patch.object(dispatch, "deliver_gws", side_effect=record_gws), mock.patch.object(
                    dispatch, "deliver_object", side_effect=record_object
                ):
                    result = dispatch.run_worker(config, connection)
                status = dispatch.build_status(config, connection)
            finally:
                connection.close()

        self.assertEqual(result, 0)
        self.assertEqual(delivered, [("gws", ["cl61/newer.nc"]), ("object", ["cl61/newer.nc"])])
        self.assertEqual(status["queue"]["dual_delivered_files"], 1)
        self.assertEqual(status["queue"]["pending_files"], 1)

    def test_radar_and_rsync_receipt_formats_extract_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            radar = root / "radar"
            radar.write_bytes(b"100.0\t42\tY2026/file.nc\0")
            rsync = root / "rsync"
            rsync.write_text(
                ">f+++++++++\t2026/camera.jpg\n.d..t......\t2026/\n",
                encoding="utf-8",
            )

            radar_paths = dispatch._paths_from_file(
                radar, null=True, records_format="radar"
            )
            rsync_paths = dispatch._paths_from_file(
                rsync, null=False, records_format="rsync"
            )

        self.assertEqual(radar_paths, ["Y2026/file.nc"])
        self.assertEqual(rsync_paths, ["2026/camera.jpg"])

    def test_destination_receipt_cannot_mark_a_newer_file_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            source = Path(config["jobs"]["raw"]["source"])
            path = source / "cl61/file.nc"
            path.parent.mkdir()
            path.write_bytes(b"old")
            connection = dispatch.connect(config)
            try:
                dispatch.enqueue_paths(
                    config,
                    connection,
                    job_name="raw",
                    base=source,
                    paths=["cl61/file.nc"],
                )
                old_row = connection.execute("SELECT * FROM delivery").fetchone()
                path.write_bytes(b"new-version")
                dispatch.enqueue_paths(
                    config,
                    connection,
                    job_name="raw",
                    base=source,
                    paths=["cl61/file.nc"],
                )
                marked = dispatch._mark_destination(
                    connection, [old_row], "gws", dispatch.utc_now()
                )
                current = connection.execute("SELECT * FROM delivery").fetchone()
            finally:
                connection.close()

        self.assertEqual(marked, 0)
        self.assertEqual(current["gws_delivered"], 0)

    def test_inaccessible_gws_and_object_store_remain_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            source = Path(config["jobs"]["raw"]["source"])
            path = source / "menapia/drone-uploads/flight.bin"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"flight")
            connection = dispatch.connect(config)
            dispatch.enqueue_paths(
                config,
                connection,
                job_name="raw",
                base=source,
                paths=["menapia/drone-uploads/flight.bin"],
            )
            try:
                with mock.patch.object(
                    dispatch, "deliver_gws", side_effect=RuntimeError("GWS unavailable")
                ), mock.patch.object(
                    dispatch,
                    "deliver_object",
                    side_effect=RuntimeError("object store unavailable"),
                ):
                    result = dispatch.run_worker(config, connection)
                row = connection.execute("SELECT * FROM delivery").fetchone()
                status = dispatch.build_status(config, connection)
            finally:
                connection.close()

        self.assertEqual(result, 1)
        self.assertEqual(row["gws_delivered"], 0)
        self.assertEqual(row["object_delivered"], 0)
        self.assertEqual(status["queue"]["gws_pending_files"], 1)
        self.assertEqual(status["queue"]["object_store_pending_files"], 1)
        self.assertEqual(status["last_run"]["state"], "failed")

    def test_menapia_subroot_keeps_the_common_raw_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            source = Path(config["jobs"]["raw"]["source"])
            menapia = source / "menapia"
            path = menapia / "drone-uploads/2026/08/24/dock-1/flight-7/data.bin"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"flight")
            connection = dispatch.connect(config)
            try:
                dispatch.enqueue_paths(
                    config,
                    connection,
                    job_name="raw",
                    base=menapia,
                    paths=["drone-uploads/2026/08/24/dock-1/flight-7/data.bin"],
                )
                relative = connection.execute(
                    "SELECT relative_path FROM delivery"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(
            relative,
            "menapia/drone-uploads/2026/08/24/dock-1/flight-7/data.bin",
        )


if __name__ == "__main__":
    unittest.main()
