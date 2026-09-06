from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "roles/object_store_mirror/files/aurora_object_store_s3.py"
SPEC = importlib.util.spec_from_file_location("object_store_s3", SCRIPT)
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)
STAMP = dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc)


class GatewayError(Exception):
    def __init__(self, code="GatewayTimeout", status=504):
        super().__init__("sensitive-url?access_key=DO-NOT-PERSIST")
        self.response = {"Error": {"Code": code, "Message": str(self)},
                         "ResponseMetadata": {"HTTPStatusCode": status}}


class FakeS3:
    """Realistic delimiter/pagination contract with deterministic late errors."""

    def __init__(self, keys=(), *, failures=None, delay=0):
        self.keys = {key: size for key, size in keys}
        self.failures = dict(failures or {})
        self.calls = []
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.delay = delay

    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        token = kwargs.get("ContinuationToken")
        with self.lock:
            self.calls.append(dict(kwargs))
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            time.sleep(self.delay)
            failure = self.failures.get((prefix, token))
            if failure:
                raise failure
            contents, dirs = [], set()
            for key, size in sorted(self.keys.items()):
                if not key.startswith(prefix):
                    continue
                relative = key[len(prefix):]
                if kwargs.get("Delimiter") and "/" in relative:
                    dirs.add(prefix + relative.split("/", 1)[0] + "/")
                else:
                    contents.append({"Key": key, "Size": size, "LastModified": STAMP})
            values = sorted([(row["Key"], "object", row) for row in contents] +
                            [(key, "prefix", {"Prefix": key}) for key in dirs])
            start = int(token or 0)
            stop = start + kwargs["MaxKeys"]
            page = values[start:stop]
            result = {"IsTruncated": stop < len(values), "Name": kwargs["Bucket"],
                      "Prefix": prefix, "KeyCount": len(page),
                      "Contents": [row for _, kind, row in page if kind == "object"],
                      "CommonPrefixes": [row for _, kind, row in page if kind == "prefix"]}
            if result["IsTruncated"]:
                result["NextContinuationToken"] = str(stop)
            return result
        finally:
            with self.lock:
                self.inflight -= 1


class PagedS3RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {"recovery_root": str(self.root), "bucket": "archive",
                       "remote": "remote", "recovery_free_reserve_bytes": 0,
                       "recovery_s3_page_size": 2}
        self.job = {"name": "products", "destination": "products", "shard_all_prefixes": True}

    def lister(self, client, epoch="epoch", verification_id="verification-1", config=None):
        result = reader.PagedS3Lister(config or self.config, self.root / epoch, verification_id, client)
        self.addCleanup(result.close)
        return result

    def local(self, *keys):
        return {key: {"relative_path": key, "size": 1, "mtime": 1, "checksum": ""} for key in keys}

    def test_late_504_restarts_from_committed_cursor_after_worker_recreation(self):
        keys = [(f"products/cl61/{n}.nc", 1) for n in range(7)]
        local = self.local(*(key.removeprefix("products/") for key, _ in keys))
        client = FakeS3(keys, failures={("products/cl61/", "4"): GatewayError()})
        first = self.lister(client)
        with self.assertRaises(reader.InventoryError) as failed:
            first.inventory(self.job, local)
        self.assertEqual(failed.exception.error_class, "transient")
        self.assertNotIn("DO-NOT-PERSIST", str(failed.exception))
        self.assertEqual(first.progress()["pages_completed"], 2)
        self.assertEqual(first.progress()["objects_observed"], 4)
        first.close()
        recovered_client = FakeS3(keys)
        recovered = self.lister(recovered_client)
        result = recovered.inventory(self.job, local)
        self.assertEqual(set(result), set(local))
        self.assertEqual([call.get("ContinuationToken") for call in recovered_client.calls], ["4", "6"])
        self.assertEqual(recovered.progress()["pages_completed"], 4)
        self.assertTrue(all(row["checksum"] == "" for row in result.values()))
        self.assertEqual(result["cl61/0.nc"]["mtime"], "2026-09-06T00:00:00Z")

    def test_completed_inventory_is_replayable_only_for_same_epoch_and_source(self):
        local = self.local("cl61/0.nc")
        original = self.lister(FakeS3([("products/cl61/0.nc", 1)]))
        expected = original.inventory(self.job, local)
        original.close()
        client = FakeS3()
        replay = self.lister(client)
        self.assertEqual(replay.inventory(self.job, local), expected)
        self.assertEqual(client.calls, [])
        replay.close()
        mismatch = self.lister(FakeS3(), verification_id="verification-2")
        with self.assertRaises(reader.InventoryError) as failed:
            mismatch.inventory(self.job, local)
        self.assertTrue(failed.exception.restart_epoch)

    def test_changed_frozen_source_or_config_cannot_reuse_pages(self):
        first = self.lister(FakeS3())
        first.inventory(self.job, self.local("cl61/a"))
        first.close()
        changed = self.lister(FakeS3())
        with self.assertRaises(reader.InventoryError) as failed:
            changed.inventory(self.job, self.local("cl61/b"))
        self.assertTrue(failed.exception.restart_epoch)
        changed.close()
        changed_config = dict(self.config, bucket="wrong-bucket")
        second = self.lister(FakeS3(), config=changed_config)
        with self.assertRaises(reader.InventoryError):
            second.inventory(self.job, self.local("cl61/a"))

    def test_invalid_token_resets_only_affected_shard(self):
        keys = [(f"products/{prefix}/{n}", 1) for prefix in ("cl61", "radar") for n in range(4)]
        local = self.local(*(key.removeprefix("products/") for key, _ in keys))
        first = self.lister(FakeS3(keys))
        first.inventory(self.job, local)
        with first.db:
            first.db.execute("DELETE FROM entries WHERE shard='products/cl61/' AND key >= 'products/cl61/2'")
            first.db.execute("DELETE FROM pages WHERE shard='products/cl61/' AND token='2'")
            first.db.execute("UPDATE shards SET cursor='expired', done=0, expanded=0, pages=1 WHERE prefix='products/cl61/'")
        first.close()
        client = FakeS3(keys, failures={("products/cl61/", "expired"): GatewayError("InvalidArgument", 400)})
        recovered = self.lister(client)
        self.assertEqual(set(recovered.inventory(self.job, local)), set(local))
        self.assertEqual([(call["Prefix"], call.get("ContinuationToken")) for call in client.calls],
                         [("products/cl61/", "expired"), ("products/cl61/", None), ("products/cl61/", "2")])

    def test_sharding_preserves_root_flat_nested_remote_only_and_exclusion_coverage(self):
        keys = [("products/root.txt", 5), ("products/cl61/flat.nc", 1),
                ("products/radar/year/month/file.nc", 1), ("products/remote-only/a", 2),
                ("products/wxcam/private/0", 1), ("products/radar/year/direct.nc", 3),
                ("products/radar/year/month/logs/no.log", 1), ("products/.cache/temp", 1)]
        local = self.local("root.txt", "cl61/flat.nc", "radar/year/month/file.nc")
        client = FakeS3(keys)
        job = dict(self.job, exclude=["wxcam/**"])
        result = self.lister(client).inventory(job, local)
        self.assertEqual(set(result), {"root.txt", "cl61/flat.nc", "radar/year/month/file.nc",
                                      "radar/year/direct.nc", "remote-only/a"})
        requests = {call["Prefix"]: call for call in client.calls}
        self.assertEqual(requests["products/"]["Delimiter"], "/")
        self.assertNotIn("Delimiter", requests["products/cl61/"])
        self.assertEqual(requests["products/radar/"]["Delimiter"], "/")
        self.assertEqual(requests["products/radar/year/"]["Delimiter"], "/")
        self.assertNotIn("Delimiter", requests["products/radar/year/month/"])
        self.assertNotIn("products/wxcam/", requests)
        self.assertNotIn("products/.cache/", requests)

    def test_source_shards_skip_pathological_root_but_cover_remote_children(self):
        local = self.local("PANO/2026/09/frame.jpg", "FISH/2026/09/frame.jpg")
        keys = [("products/" + key, 1) for key in local] + [("products/PANO/2025/08/older.jpg", 1)]
        client = FakeS3(keys)
        job = {"name": "products-wxcam", "destination": "products", "sharded_prefixes": ["PANO", "FISH"]}
        result = self.lister(client).inventory(job, local)
        self.assertEqual(len(result), 3)
        self.assertFalse(any(call["Prefix"] == "products/" for call in client.calls))

    def test_empty_source_still_discovers_remote_tree(self):
        client = FakeS3([("products/remote/year/file", 1)])
        result = self.lister(client).inventory(self.job, {})
        self.assertEqual(set(result), {"remote/year/file"})

    def test_two_request_limit_even_with_many_nested_shards(self):
        keys = [(f"products/p{n}/2026/{month}/file", 1) for n in range(6) for month in range(4)]
        local = self.local(*(key.removeprefix("products/") for key, _ in keys))
        client = FakeS3(keys, delay=0.001)
        self.assertEqual(len(self.lister(client).inventory(self.job, local)), len(keys))
        self.assertEqual(client.peak, 2)
        self.assertTrue(all(call["MaxKeys"] <= 1000 for call in client.calls))

    def test_malformed_pages_never_advance_checkpoint(self):
        cases = [None, {}, {"IsTruncated": "false"},
                 {"IsTruncated": True, "Contents": []},
                 {"IsTruncated": False, "Contents": None},
                 {"IsTruncated": False, "Contents": [{"Key": "products/a"}]},
                 {"IsTruncated": False, "Contents": [{"Key": "wrong/a", "Size": 1, "LastModified": STAMP}]},
                 {"IsTruncated": False, "Contents": [{"Key": "products/a", "Size": -1, "LastModified": STAMP}]},
                 {"IsTruncated": False, "Contents": [{"Key": "products/a", "Size": 1, "LastModified": STAMP.replace(tzinfo=None)}]},
                 {"IsTruncated": False, "Contents": [], "KeyCount": 9},
                 {"IsTruncated": False, "Contents": [], "NextContinuationToken": "bogus"},
                 {"IsTruncated": False, "CommonPrefixes": [{"Prefix": "products/child/grandchild/"}]}]
        for index, response in enumerate(cases):
            with self.subTest(index=index):
                client = SimpleNamespace(list_objects_v2=mock.Mock(return_value=response))
                lister = self.lister(client, epoch=f"malformed-{index}")
                with self.assertRaises(reader.InventoryError):
                    lister.inventory(self.job, {})
                self.assertEqual(lister.progress()["pages_completed"], 0)
                self.assertEqual(lister.progress()["objects_observed"], 0)

    def test_transaction_rolls_back_rows_and_cursor_together(self):
        lister = self.lister(FakeS3())
        lister._initialize(self.job, self.local("cl61/0"))
        with lister.db:
            lister.db.execute("CREATE TRIGGER fail_cursor BEFORE UPDATE ON shards BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            lister._commit_page("products/cl61/", None, [("products/cl61/0", "object", 1, "time")], "2", False)
        self.assertEqual(lister.progress()["objects_observed"], 0)
        self.assertEqual(lister.db.execute("SELECT cursor FROM shards").fetchone()[0], None)
        self.assertEqual(lister.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 0)

    def test_identical_page_replay_idempotent_conflicting_replay_rejected(self):
        lister = self.lister(FakeS3())
        lister._initialize(self.job, self.local("cl61/0"))
        rows = [("products/cl61/0", "object", 1, "time")]
        lister._commit_page("products/cl61/", None, rows, "2", False)
        lister._commit_page("products/cl61/", None, rows, "2", False)
        self.assertEqual(lister.progress()["pages_completed"], 1)
        with self.assertRaises(reader.InventoryError) as failed:
            lister._commit_page("products/cl61/", None, [("products/cl61/0", "object", 2, "time")], "2", False)
        self.assertTrue(failed.exception.restart_epoch)
        self.assertEqual(lister.db.execute("SELECT size FROM entries").fetchone()[0], 1)

    def test_conflicting_object_across_pages_and_token_cycles_rejected(self):
        lister = self.lister(FakeS3())
        lister._initialize(self.job, self.local("cl61/0"))
        lister._commit_page("products/cl61/", None, [("products/cl61/0", "object", 1, "time")], "2", False)
        with self.assertRaises(reader.InventoryError) as failed:
            lister._commit_page("products/cl61/", "2", [("products/cl61/0", "object", 2, "time")], "4", False)
        self.assertTrue(failed.exception.restart_epoch)
        lister._commit_page("products/cl61/", "2", [], "4", False)
        with self.assertRaises(reader.InventoryError):
            lister._commit_page("products/cl61/", "4", [], "2", False)
        self.assertEqual(lister.progress()["pages_completed"], 2)

    def test_storage_quota_includes_other_epochs_and_queue_journals(self):
        (self.root / "queue.sqlite-wal").write_bytes(b"x" * 4096)
        (self.root / "older-epoch").mkdir()
        (self.root / "older-epoch/source.json").write_bytes(b"x" * 8192)
        config = dict(self.config, recovery_cache_max_bytes=12000)
        with self.assertRaises(reader.InventoryError) as failed:
            self.lister(FakeS3(), config=config)
        self.assertEqual(failed.exception.error_class, "resource")
        self.assertFalse((self.root / "epoch/s3.sqlite").exists())

    def test_low_free_space_does_not_create_or_advance_checkpoint(self):
        config = dict(self.config, recovery_free_reserve_bytes=50 * 1024**3)
        with mock.patch.object(reader.shutil, "disk_usage", return_value=SimpleNamespace(free=49 * 1024**3)):
            with self.assertRaises(reader.InventoryError) as failed:
                self.lister(FakeS3(), config=config)
        self.assertEqual(failed.exception.error_class, "resource")

    def test_quota_exhaustion_after_first_page_preserves_that_page(self):
        local = self.local("cl61/0", "cl61/1", "cl61/2")
        client = FakeS3([("products/" + key, 1) for key in local])
        config = dict(self.config)
        observed = []

        def fill_storage(progress):
            observed.append(progress)
            (self.root / "queue.sqlite-wal").write_bytes(b"x" * 1024)
            config["recovery_cache_max_bytes"] = 1

        config["_progress_callback"] = fill_storage
        lister = self.lister(client, config=config)
        with self.assertRaises(reader.InventoryError) as failed:
            lister.inventory(self.job, local)
        self.assertEqual(failed.exception.error_class, "resource")
        self.assertEqual(lister.progress()["pages_completed"], 1)
        self.assertEqual(lister.progress()["objects_observed"], 2)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(observed[0]["objects_observed"], 2)

    def test_deadline_before_page_and_after_response_never_certifies_expired_scan(self):
        client = FakeS3()
        config = dict(self.config, _recovery_deadline=time.time() - 1)
        lister = self.lister(client, config=config)
        with self.assertRaises(reader.InventoryError) as failed:
            lister.inventory(self.job, {})
        self.assertTrue(failed.exception.restart_epoch)
        self.assertEqual(client.calls, [])
        config["_recovery_deadline"] = time.time() + 600
        original = client.list_objects_v2

        def expire_during_request(**kwargs):
            config["_recovery_deadline"] = time.time() - 1
            return original(**kwargs)

        client.list_objects_v2 = expire_during_request
        with self.assertRaises(reader.InventoryError) as failed:
            lister.inventory(self.job, {})
        self.assertTrue(failed.exception.restart_epoch)
        self.assertEqual(lister.progress()["pages_completed"], 0)

    def test_repair_invalidation_aborts_page_and_private_callbacks_do_not_change_fingerprint(self):
        client = FakeS3()
        valid = [True]
        config = dict(self.config, _validate_epoch=lambda: valid[0])
        lister = self.lister(client, config=config)
        original = client.list_objects_v2

        def invalidate_during_request(**kwargs):
            valid[0] = False
            return original(**kwargs)

        client.list_objects_v2 = invalidate_during_request
        with self.assertRaises(reader.InventoryError) as failed:
            lister.inventory(self.job, {})
        self.assertTrue(failed.exception.restart_epoch)
        self.assertEqual(lister.progress()["pages_completed"], 0)
        lister.close()
        resumed = self.lister(FakeS3(), config=dict(self.config, _validate_epoch=lambda: True))
        self.assertEqual(resumed.inventory(self.job, {}), {})

    def test_corrupt_checkpoint_is_never_accepted_as_empty_inventory(self):
        (self.root / "epoch").mkdir()
        (self.root / "epoch/s3.sqlite").write_bytes(b"not a sqlite database")
        with self.assertRaises(reader.InventoryError) as failed:
            self.lister(FakeS3())
        self.assertTrue(failed.exception.restart_epoch)

    def test_credentials_are_explicit_restricted_and_sdk_retries_disabled(self):
        credential_dir = self.root / "credentials"
        credential_dir.mkdir()
        credential = credential_dir / "s3-rclone-config"
        credential.write_text("[remote]\ntype=s3\nprovider=Other\naccess_key_id=TEST-ACCESS\nsecret_access_key=TEST-SECRET\nendpoint=s3.example.invalid\n")
        credential.chmod(0o400)
        boto = SimpleNamespace(client=mock.Mock(return_value="CLIENT"))
        config_ctor = mock.Mock(side_effect=lambda **kwargs: kwargs)
        with mock.patch.dict(sys.modules, {"boto3": boto, "botocore.config": SimpleNamespace(Config=config_ctor)}), mock.patch.dict(
                reader.os.environ, {"CREDENTIALS_DIRECTORY": str(credential_dir)}):
            self.assertEqual(reader._client(self.config), "CLIENT")
        kwargs = boto.client.call_args.kwargs
        self.assertEqual(kwargs["aws_access_key_id"], "TEST-ACCESS")
        self.assertEqual(kwargs["aws_secret_access_key"], "TEST-SECRET")
        self.assertEqual(kwargs["region_name"], "us-east-1")
        self.assertEqual(kwargs["endpoint_url"], "https://s3.example.invalid")
        self.assertEqual(kwargs["config"]["retries"], {"total_max_attempts": 1, "mode": "standard"})
        self.assertEqual(kwargs["config"]["connect_timeout"], 30)
        self.assertEqual(kwargs["config"]["read_timeout"], 120)
        self.assertEqual(kwargs["config"]["s3"]["addressing_style"], "path")
        self.assertEqual(kwargs["config"]["max_pool_connections"], 2)

    def test_insecure_endpoint_ambient_auth_or_permissions_are_rejected_without_sdk(self):
        credential_dir = self.root / "credentials"
        credential_dir.mkdir()
        credential = credential_dir / "s3-rclone-config"
        base = "[remote]\ntype=s3\nprovider=Other\naccess_key_id=TEST-ACCESS\nsecret_access_key=TEST-SECRET\n"
        for settings, mode in [("endpoint=http://s3.example.invalid\n", 0o600),
                               ("endpoint=s3.example.invalid\nenv_auth=true\n", 0o600),
                               ("endpoint=s3.example.invalid\n", 0o640)]:
            with self.subTest(settings=settings, mode=mode):
                credential.write_text(base + settings)
                credential.chmod(mode)
                with mock.patch.dict(reader.os.environ, {"CREDENTIALS_DIRECTORY": str(credential_dir)}):
                    with self.assertRaises(reader.InventoryError) as failed:
                        reader._client(self.config)
                self.assertEqual(failed.exception.error_class, "config")
                self.assertNotIn("TEST-SECRET", str(failed.exception))

    def test_systemd_missing_credential_never_falls_back_to_other_credentials(self):
        config = dict(self.config, rclone_config="/does-not-matter")
        with mock.patch.dict(reader.os.environ, {"CREDENTIALS_DIRECTORY": str(self.root)}):
            with self.assertRaises(reader.InventoryError) as failed:
                reader._client(config)
        self.assertEqual(failed.exception.error_class, "config")

    def test_auth_and_configuration_failures_are_permanent_and_sanitized(self):
        for code, status, expected in [("AccessDenied", 403, "auth"), ("NoSuchBucket", 404, "config"),
                                       ("GatewayTimeout", 504, "transient")]:
            with self.subTest(code=code):
                error, invalid = reader._request_error(GatewayError(code, status), has_cursor=False)
                self.assertEqual(error.error_class, expected)
                self.assertFalse(invalid)
                self.assertNotIn("DO-NOT-PERSIST", str(error))


if __name__ == "__main__":
    unittest.main()
