from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = (
    Path(__file__).parents[1]
    / "roles"
    / "object_store_mirror"
    / "files"
    / "aurora-object-store-inventory.py"
)
SPEC = importlib.util.spec_from_file_location("object_store_inventory", SCRIPT)
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


class ObjectStoreInventoryTests(unittest.TestCase):
    def test_incremental_base_can_refresh_one_family_from_old_deep_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest = root / "latest"
            latest.mkdir()
            report = {
                "generated_at": (
                    dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
                ).isoformat(),
                "jobs": {"raw": {}, "products": {}},
                "incremental_depth": 99,
            }
            (latest / "comparison.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            config = {
                "jobs": [{"name": "raw"}, {"name": "products"}],
            }

            loaded, digest = inventory.load_incremental_base(
                root, config, {"products"}
            )
            self.assertEqual(loaded["incremental_depth"], 99)
            self.assertEqual(set(loaded["jobs"]), {"raw", "products"})
            self.assertEqual(len(digest), 64)
            self.assertEqual(
                loaded["jobs"]["raw"]["verified_at"],
                report["generated_at"],
            )

    def test_systemd_credentials_override_catalog_key_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credentials = Path(tmp)
            key = credentials / "gws-key"
            known_hosts = credentials / "gws-known-hosts"
            key.write_text("key", encoding="utf-8")
            known_hosts.write_text("known", encoding="utf-8")
            config = {
                "gws_key": "/missing/key",
                "gws_known_hosts": "/missing/known-hosts",
            }
            with mock.patch.dict(
                inventory.os.environ,
                {"CREDENTIALS_DIRECTORY": str(credentials)},
            ):
                self.assertEqual(
                    inventory.credential_path(config, "gws_key", "gws-key"),
                    str(key),
                )
                self.assertEqual(
                    inventory.credential_path(
                        config, "gws_known_hosts", "gws-known-hosts"
                    ),
                    str(known_hosts),
                )

    def test_non_raw_gws_inventory_is_listed_independently(self) -> None:
        completed = SimpleNamespace(
            stdout="stable/file.nc\t42\t100.5\nlogs/skip.log\t7\t100\n"
        )
        config = {
            "gws_key": "/key",
            "gws_known_hosts": "/known_hosts",
            "gws_user": "user",
            "gws_hosts": ["xfer.example"],
        }
        job = {
            "name": "products",
            "gws_destination": "/gws/products",
        }
        with mock.patch.object(
            inventory.subprocess,
            "run",
            return_value=completed,
        ) as run:
            result = inventory.gws_inventory(config, job)

        self.assertEqual(result["stable/file.nc"]["size"], 42)
        self.assertNotIn("logs/skip.log", result)
        command = run.call_args.args[0]
        self.assertIn("user@xfer.example", command)
        self.assertIn("cd \"/gws/products\"", command[-1])
        self.assertIn("Compression=yes", command)
        self.assertIn("UserKnownHostsFile=/known_hosts", command)

    def test_gws_inventory_prunes_fully_excluded_history_on_remote(self) -> None:
        completed = SimpleNamespace(stdout="latest/summary.json\t42\t100.5\n")
        config = {
            "gws_key": "/key",
            "gws_user": "user",
            "gws_hosts": ["xfer.example"],
        }
        job = {
            "name": "manifests",
            "gws_destination": "/gws/manifests",
            "exclude": ["history/**", "logs/**"],
        }
        with mock.patch.object(
            inventory.subprocess,
            "run",
            return_value=completed,
        ) as run:
            inventory.gws_inventory(config, job)

        remote_command = run.call_args.args[0][-1]
        self.assertIn('"./history"', remote_command)
        self.assertIn('"./logs"', remote_command)
        self.assertIn('"-prune"', remote_command)

    def test_gws_inventory_retries_the_host_pool(self) -> None:
        completed = SimpleNamespace(stdout="stable/file.nc\t42\t100.5\n")
        config = {
            "gws_key": "/key",
            "gws_user": "user",
            "gws_hosts": ["xfer-1.example", "xfer-2.example"],
            "gws_inventory_attempts": 2,
            "gws_inventory_retry_delay_seconds": 0,
        }
        job = {
            "name": "products",
            "gws_destination": "/gws/products",
        }
        failure = subprocess.CalledProcessError(
            255,
            ["ssh"],
            stderr="temporary connection failure",
        )
        with mock.patch.object(
            inventory.subprocess,
            "run",
            side_effect=[failure, failure, completed],
        ) as run:
            result = inventory.gws_inventory(config, job)

        self.assertEqual(result["stable/file.nc"]["size"], 42)
        self.assertEqual(run.call_count, 3)

    def test_publish_keeps_bounded_immutable_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(3):
                stage = root / f"stage-{index}"
                stage.mkdir()
                (stage / "comparison.json").write_text(
                    str(index),
                    encoding="utf-8",
                )
                inventory.publish(
                    root,
                    stage,
                    f"2026-07-26T00:0{index}:00Z",
                    history_keep=2,
                )

            snapshots = sorted(
                path.name for path in (root / "history").iterdir()
            )

        self.assertEqual(
            snapshots,
            ["20260726T000100Z", "20260726T000200Z"],
        )

    def test_job_checkpoint_refreshes_only_successful_family(self) -> None:
        def job(verified_at: str, count: int) -> dict:
            comparison = {
                "left_count": count,
                "right_count": count,
                "missing_from_right": [],
                "extra_in_right": [],
                "size_mismatch": [],
                "checksum_mismatch": [],
                "matches": count,
            }
            return {
                "source_vs_s3": comparison,
                "source_vs_gws": comparison,
                "verified_at": verified_at,
                "verification_scope": "full_family",
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest = root / "latest"
            latest.mkdir()
            old = "2026-08-16T00:00:00Z"
            base = {
                "schema_version": 5,
                "generated_at": old,
                "verification_mode": "full",
                "verified_jobs": ["raw", "products"],
                "evidence_floor_generated_at": old,
                "incremental_depth": 0,
                "jobs": {
                    "raw": job(old, 1),
                    "products": job(old, 2),
                },
            }
            (latest / "comparison.json").write_text(
                json.dumps(base, indent=2) + "\n", encoding="utf-8"
            )
            (latest / "comparison.md").write_text("old\n", encoding="utf-8")
            (latest / "catalog.json").write_text("{}\n", encoding="utf-8")
            stage = root / "stage"
            stage.mkdir()
            (stage / "raw-local.tsv").write_text("new\n", encoding="utf-8")
            fresh = job("2026-08-19T12:00:00Z", 3)
            digest = inventory.hashlib.sha256(
                (latest / "comparison.json").read_bytes()
            ).hexdigest()

            checkpoint, _digest = inventory.publish_job_checkpoint(
                root=root,
                stage=stage,
                config={"streams": [], "history_keep": 12},
                base_report=base,
                base_report_sha256=digest,
                name="raw",
                values=fresh,
                verified_jobs={"raw"},
            )
            published = json.loads(
                (latest / "comparison.json").read_text(encoding="utf-8")
            )

        self.assertEqual(checkpoint["verification_mode"], "incremental")
        self.assertEqual(published["verified_jobs"], ["raw"])
        self.assertEqual(published["jobs"]["raw"]["source_vs_s3"]["matches"], 3)
        self.assertEqual(
            published["jobs"]["products"]["verified_at"], old
        )

    def test_verification_horizon_is_independent_of_writer_settle_age(self) -> None:
        self.assertEqual(
            inventory.verification_settle_age(
                {"settle_age": "15m", "verification_settle_age": "6h"}
            ),
            "6h",
        )
        self.assertEqual(
            inventory.verification_settle_age({"settle_age": "15m"}),
            "15m",
        )

    def test_local_inventory_excludes_symlink_pointers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "20260725.mp4"
            target.write_bytes(b"video")
            (root / "latest.mp4").symlink_to(target.name)

            result = inventory.local_inventory(str(root), [], "0s")

        self.assertIn("20260725.mp4", result)
        self.assertNotIn("latest.mp4", result)

    def test_local_inventory_includes_dereferenced_archive_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "runtime.dat"
            target.write_bytes(b"archive me")
            (root / "linked.dat").symlink_to(target.name)

            result = inventory.local_inventory(
                str(root), [], "0s", copy_links=True
            )

        self.assertEqual(result["linked.dat"]["size"], len(b"archive me"))

    def test_local_inventory_prunes_fully_excluded_directories(self) -> None:
        entered: list[str] = []

        def fake_walk(root: Path):
            dirnames = ["wxcam.zarr", "daily_videos"]
            yield str(root), dirnames, []
            if "wxcam.zarr" in dirnames:
                entered.append("wxcam.zarr")
                yield str(Path(root) / "wxcam.zarr"), [], ["chunk"]
            if "daily_videos" in dirnames:
                entered.append("daily_videos")
                yield str(Path(root) / "daily_videos"), [], []

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            inventory.os, "walk", side_effect=fake_walk
        ):
            inventory.local_inventory(
                tmp,
                ["wxcam.zarr/**"],
                "0s",
            )

        self.assertEqual(entered, ["daily_videos"])

    def test_remote_window_recheck_excludes_mutated_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "daily.mp4"
            path.write_bytes(b"old")
            rows = inventory.local_inventory(str(root), [], "0s")
            path.write_bytes(b"new-content")

            stable = inventory.retain_unchanged_local_snapshot(str(root), rows)

        self.assertNotIn("daily.mp4", stable)

    def test_remote_window_recheck_keeps_unchanged_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "complete.nc"
            path.write_bytes(b"stable")
            rows = inventory.local_inventory(str(root), [], "0s")

            stable = inventory.retain_unchanged_local_snapshot(str(root), rows)

        self.assertEqual(stable, rows)

    def test_shard_all_prefixes_avoids_one_recursive_family_listing(self) -> None:
        calls: list[tuple[str, bool, bool]] = []
        lister = inventory.S3Lister(
            {
                "remote": "remote",
                "bucket": "bucket",
                "list_workers": 2,
            }
        )

        def fake_list_json(
            remote: str, *, recursive: bool = True, files_only: bool = True
        ) -> list[dict]:
            calls.append((remote, recursive, files_only))
            if remote == "remote:bucket/products":
                return [{"Path": "cl61", "IsDir": True}]
            if remote.endswith("/cl61"):
                return [{"Path": "store.zarr", "IsDir": True}]
            if remote.endswith("/cl61/store.zarr"):
                return [{"Path": "array", "IsDir": True}]
            if remote.endswith("/cl61/store.zarr/array"):
                return [{"Path": "0", "IsDir": False, "Size": 42}]
            raise AssertionError(f"unexpected listing: {remote}")

        lister.list_json = fake_list_json  # type: ignore[method-assign]
        result = lister.inventory(
            {
                "destination": "products",
                "shard_all_prefixes": True,
            },
            {
                "cl61/store.zarr/array/0": {
                    "relative_path": "cl61/store.zarr/array/0",
                    "size": 42,
                }
            },
        )

        self.assertEqual(list(result), ["cl61/store.zarr/array/0"])
        self.assertNotIn(
            ("remote:bucket/products/cl61", True, True),
            calls,
        )

    def test_gws_inventory_uses_canonical_archive_relpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "latest" / "asfs_fast_sonic" / "gws.tsv"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                "relpath\tsize\tmtime\tchecksum\n"
                "fast_sonic_20260725.nc\t42\t100\tabc\n",
                encoding="utf-8",
            )
            config = {
                "gws_manifest_root": str(root),
                "streams": [
                    {
                        "name": "asfs_fast_sonic",
                        "archive_relpath": "asfs/crd",
                    }
                ],
            }

            result = inventory.gws_inventory(config, {"name": "raw"})

        self.assertEqual(
            list(result),
            ["asfs/crd/fast_sonic_20260725.nc"],
        )
        self.assertEqual(result["asfs/crd/fast_sonic_20260725.nc"]["size"], 42)

    def test_gws_comparison_uses_canonical_source_and_gws_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "latest" / "hatprog5"
            directory.mkdir(parents=True)
            rows = (
                "relpath\tsize\tmtime\tchecksum\n"
                "Y2026/M07/D25/file.NC\t42\t100\t\n"
            )
            (directory / "source.tsv").write_text(rows, encoding="utf-8")
            (directory / "gws.tsv").write_text(rows, encoding="utf-8")
            config = {
                "gws_manifest_root": str(root),
                "streams": [
                    {
                        "name": "hatprog5",
                        "archive_relpath": "hatprog5",
                    }
                ],
            }
            job = {"name": "raw"}

            source = inventory.mirror_manifest_inventory(
                config, job, "source"
            )
            gws = inventory.mirror_manifest_inventory(config, job, "gws")

        self.assertEqual(inventory.compare(source, gws)["missing_from_right"], [])
        self.assertIn("hatprog5/Y2026/M07/D25/file.NC", source)

    def test_gws_comparison_does_not_flatten_canonical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "latest" / "hatprog5"
            directory.mkdir(parents=True)
            (directory / "source.tsv").write_text(
                "relpath\tsize\tmtime\tchecksum\n"
                "Y2026/M07/D25/file.NC\t42\t100\t\n",
                encoding="utf-8",
            )
            config = {
                "gws_manifest_root": str(root),
                "streams": [
                    {
                        "name": "hatprog5",
                        "archive_relpath": "hatprog5",
                    }
                ],
            }

            source = inventory.mirror_manifest_inventory(
                config, {"name": "raw"}, "source"
            )

        self.assertNotIn("hatprog5/file.NC", source)

    def test_gws_comparison_uses_immutable_history_and_settle_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latest = root / "latest"
            latest.mkdir(parents=True)
            (latest / "summary.json").write_text(
                '{"generated_at":"2026-07-25T12:00:00+00:00"}\n',
                encoding="utf-8",
            )
            directory = root / "history" / "20260725T120000Z" / "cl61"
            directory.mkdir(parents=True)
            rows = (
                "relpath\tsize\tmtime\tchecksum\n"
                "old.nc\t42\t1784970000\t\n"
                "live.nc\t43\t1784980700\t\n"
            )
            (directory / "source.tsv").write_text(rows, encoding="utf-8")
            (directory / "gws.tsv").write_text(rows, encoding="utf-8")
            config = {
                "gws_manifest_root": str(root),
                "gws_settle_seconds": 2700,
                "streams": [
                    {
                        "name": "cl61",
                        "archive_relpath": "cl61",
                    }
                ],
            }

            source = inventory.mirror_manifest_inventory(
                config, {"name": "raw"}, "source"
            )
            gws = inventory.mirror_manifest_inventory(
                config, {"name": "raw"}, "gws"
            )

        self.assertIn("cl61/old.nc", source)
        self.assertNotIn("cl61/live.nc", source)
        self.assertIn("cl61/live.nc", gws)

    def test_shard_all_prefixes_preserves_files_in_flat_family(self) -> None:
        lister = inventory.S3Lister(
            {
                "remote": "remote",
                "bucket": "bucket",
                "list_workers": 2,
                "shard_list_workers": 4,
            }
        )

        def fake_list_json(
            remote: str, *, recursive: bool = True, files_only: bool = True
        ) -> list[dict]:
            if remote == "remote:bucket/raw":
                return [{"Path": "cl61", "IsDir": True}]
            if remote.endswith("/cl61"):
                self.assertTrue(recursive)
                self.assertTrue(files_only)
                return [
                    {
                        "Path": "ceilometer_20260725.nc",
                        "IsDir": False,
                        "Size": 42,
                    }
                ]
            raise AssertionError(f"unexpected listing: {remote}")

        lister.list_json = fake_list_json  # type: ignore[method-assign]
        result = lister.inventory(
            {
                "destination": "raw",
                "shard_all_prefixes": True,
            },
            {
                "cl61/ceilometer_20260725.nc": {
                    "relative_path": "cl61/ceilometer_20260725.nc",
                    "size": 42,
                }
            },
        )

        self.assertEqual(list(result), ["cl61/ceilometer_20260725.nc"])
        self.assertEqual(result["cl61/ceilometer_20260725.nc"]["size"], 42)

    def test_global_listing_limit_caps_nested_concurrency(self) -> None:
        lister = inventory.S3Lister(
            {
                "remote": "remote",
                "bucket": "bucket",
                "rclone_config": "/config",
                "list_process_limit": 2,
            }
        )
        lock = threading.Lock()
        active = 0
        maximum = 0

        def fake_run(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return SimpleNamespace(stdout="[]")

        with mock.patch.object(inventory.subprocess, "run", side_effect=fake_run):
            with inventory.ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lister.list_json, [f"remote:{i}" for i in range(6)]))

        self.assertEqual(maximum, 2)

    def test_listing_timeout_is_configurable_for_large_valid_shards(self) -> None:
        lister = inventory.S3Lister(
            {
                "remote": "remote",
                "bucket": "bucket",
                "rclone_config": "/config",
                "list_timeout_seconds": 3600,
            }
        )

        with mock.patch.object(
            inventory.subprocess,
            "run",
            return_value=SimpleNamespace(stdout="[]"),
        ) as run:
            lister.list_json("remote:large-shard")

        self.assertEqual(run.call_args.kwargs["timeout"], 3600)

    def test_s3_listing_retries_after_transient_gateway_failure(self) -> None:
        lister = inventory.S3Lister(
            {
                "remote": "remote",
                "bucket": "bucket",
                "rclone_config": "/config",
                "s3_list_attempts": 2,
                "s3_list_retry_delay_seconds": 0,
            }
        )
        failure = subprocess.CalledProcessError(
            1,
            ["/usr/bin/rclone", "lsjson"],
        )

        with mock.patch.object(
            inventory.subprocess,
            "run",
            side_effect=[failure, SimpleNamespace(stdout="[]")],
        ) as run:
            result = lister.list_json("remote:transient-shard")

        self.assertEqual(result, [])
        self.assertEqual(run.call_count, 2)

    def test_fully_configured_shards_skip_pathological_root_listing(self) -> None:
        lister = inventory.S3Lister(
            {
                "remote": "remote",
                "bucket": "bucket",
                "rclone_config": "/config",
                "list_workers": 1,
                "shard_list_workers": 1,
            }
        )
        calls: list[str] = []

        def fake_list_json(
            remote: str, *, recursive: bool = True, files_only: bool = True
        ) -> list[dict]:
            calls.append(remote)
            if remote.endswith("/FISH") or remote.endswith("/PANO"):
                return []
            raise AssertionError(f"unexpected root listing: {remote}")

        lister.list_json = fake_list_json  # type: ignore[method-assign]
        result = lister.inventory(
            {
                "name": "products-wxcam",
                "destination": "products/wxcam",
                "sharded_prefixes": ["FISH", "PANO"],
            },
            {
                "FISH/20260728/frame.jpg": {
                    "relpath": "FISH/20260728/frame.jpg",
                    "size": 1,
                    "mtime": 1,
                    "checksum": None,
                },
                "PANO/20260728/frame.jpg": {
                    "relpath": "PANO/20260728/frame.jpg",
                    "size": 1,
                    "mtime": 1,
                    "checksum": None,
                },
            },
        )

        self.assertEqual(result, {})
        self.assertNotIn("remote:bucket/products/wxcam", calls)
        self.assertEqual(
            calls,
            [
                "remote:bucket/products/wxcam/FISH",
                "remote:bucket/products/wxcam/PANO",
            ],
        )

    def test_remote_excluded_family_is_not_listed(self) -> None:
        calls: list[str] = []
        lister = inventory.S3Lister(
            {
                "remote": "remote",
                "bucket": "bucket",
                "list_workers": 2,
            }
        )

        def fake_list_json(
            remote: str, *, recursive: bool = True, files_only: bool = True
        ) -> list[dict]:
            calls.append(remote)
            if remote == "remote:bucket/products":
                return [
                    {"Path": "cl61", "IsDir": True},
                    {"Path": "wxcam", "IsDir": True},
                ]
            if remote.endswith("/cl61"):
                return [{"Path": "store.zarr", "IsDir": True}]
            if remote.endswith("/cl61/store.zarr"):
                return [{"Path": "array", "IsDir": True}]
            if remote.endswith("/cl61/store.zarr/array"):
                return [{"Path": "0", "IsDir": False, "Size": 42}]
            raise AssertionError(f"excluded family was listed: {remote}")

        lister.list_json = fake_list_json  # type: ignore[method-assign]
        result = lister.inventory(
            {
                "destination": "products",
                "shard_all_prefixes": True,
                "exclude": ["wxcam/**"],
            },
            {
                "cl61/store.zarr/array/0": {
                    "relative_path": "cl61/store.zarr/array/0",
                    "size": 42,
                }
            },
        )

        self.assertEqual(list(result), ["cl61/store.zarr/array/0"])
        self.assertFalse(any(remote.endswith("/wxcam") for remote in calls))

    def test_progress_publication_is_atomic_and_replaces_previous_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory.write_progress(
                root,
                {
                    "state": "running",
                    "current_job": "raw",
                },
            )
            inventory.write_progress(
                root,
                {
                    "state": "complete",
                    "current_job": None,
                },
            )

            progress = (root / "progress.json").read_text(encoding="utf-8")

        self.assertIn('"state": "complete"', progress)
        self.assertNotIn('"state": "running"', progress)

    def test_non_raw_jobs_do_not_claim_gws_evidence(self) -> None:
        self.assertEqual(
            inventory.gws_inventory(
                {"gws_manifest_root": "/does/not/matter"},
                {"name": "products"},
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
