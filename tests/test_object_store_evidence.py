from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "roles/object_store_mirror/files/aurora_object_store_evidence.py"
SPEC = importlib.util.spec_from_file_location("aurora_evidence_test", SCRIPT)
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)


class EvidencePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cfg = {"manifest_root": str(self.root / "manifest"),
            "gate_state_path": str(self.root / "state/gate.json"),
            "gws_manifest_root": str(self.root / "gws"),
            "jobs": [{"name": "raw"}, {"name": "products"}], "streams": [],
            "history_keep": 2, "recovery_enabled": True,
            "recovery_free_reserve_bytes": 0, "recovery_cache_max_bytes": 64 * 1024**2}
        gws = self.root / "gws/latest"
        gws.mkdir(parents=True)
        (gws / "summary.json").write_text(json.dumps({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "streams": {}}))

    def values(self, name, number):
        stamp = dt.datetime.now(dt.timezone.utc)
        comparison = {"missing_from_right": [], "size_mismatch": [], "checksum_mismatch": []}
        return {"verification_id": f"{name}-{number}", "evidence_started_at": (stamp - dt.timedelta(minutes=1)).isoformat(),
                "verification_completed_at": stamp.isoformat(), "verification_scope": "full_family",
                "source_vs_s3": comparison, "source_vs_gws": comparison}

    def artifacts(self, name, number):
        root = self.root / f"stage-{name}-{number}"
        root.mkdir(exist_ok=True)
        for suffix in ("local", "s3", "gws"):
            (root / f"{name}-{suffix}.tsv").write_text(f"path\tsize\tmtime\n{name}-{number}\t1\t0\n")
        return root

    def publish(self, name, number):
        return evidence.publish_family(self.cfg, name, self.values(name, number), self.artifacts(name, number))

    def test_family_merge_publishes_consistent_report_gate_and_artifacts(self):
        self.publish("raw", 1)
        first, state = self.publish("products", 1)
        self.assertTrue(state["clean"])
        self.assertFalse(state["stable_parity"])
        with evidence.read_snapshot(self.cfg) as snapshot:
            self.assertEqual(set(snapshot.report["jobs"]), {"raw", "products"})
            self.assertEqual(snapshot.report_sha256, snapshot.gate["report_sha256"])
            self.assertIn("raw-1", (snapshot.path / "raw-local.tsv").read_text())
            self.assertIn("products-1", (snapshot.path / "products-local.tsv").read_text())
            self.assertEqual(snapshot.report["jobs"]["raw"]["verified_at"], snapshot.report["jobs"]["raw"]["evidence_started_at"])
        self.publish("products", 2)
        _, state = self.publish("raw", 2)
        self.assertTrue(state["stable_parity"])

    def test_parallel_families_merge_against_latest_commit(self):
        def worker(name):
            values = self.values(name, 1)
            artifacts = self.artifacts(name, 1)
            for _ in range(100):
                try:
                    return evidence.publish_family(self.cfg, name, values, artifacts)
                except BlockingIOError:
                    time.sleep(0.001)
            self.fail("commit lock never released")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(worker, ["raw", "products"]))
        with evidence.read_snapshot(self.cfg) as snapshot:
            self.assertEqual(set(snapshot.report["jobs"]), {"raw", "products"})
            self.assertTrue(snapshot.gate["clean"])

    def test_products_publication_preserves_products_wxcam_artifacts(self):
        self.cfg["jobs"].append({"name": "products-wxcam"})
        self.publish("products-wxcam", 1)
        self.publish("products", 1)
        self.publish("products", 2)
        with evidence.read_snapshot(self.cfg) as snapshot:
            self.assertIn("products-wxcam-1", (snapshot.path / "products-wxcam-local.tsv").read_text())
            self.assertEqual(snapshot.report["jobs"]["products-wxcam"]["verification_id"], "products-wxcam-1")

    def test_restrictive_umask_does_not_hide_canonical_generations(self):
        mask = os.umask(0o077)
        try:
            self.publish("raw", 1)
            evidence.refresh_gate(self.cfg)
        finally:
            os.umask(mask)
        root = Path(self.cfg["manifest_root"])
        generation = (root / "latest").resolve()
        self.assertEqual((root / "generations").stat().st_mode & 0o777, 0o755)
        self.assertEqual(generation.stat().st_mode & 0o777, 0o755)
        for name in ("comparison.json", "verification-gate.json", ".pin.lock", "raw-local.tsv"):
            self.assertEqual((generation / name).stat().st_mode & 0o777, 0o644)

    def test_readonly_snapshot_never_opens_commit_lock_for_writes(self):
        self.publish("raw", 1)
        original_open = Path.open
        def checked_open(path, mode="r", *args, **kwargs):
            if path.name == ".inventory.lock":
                self.assertEqual(mode, "rb")
            return original_open(path, mode, *args, **kwargs)
        with mock.patch.object(Path, "open", checked_open):
            with evidence.read_snapshot(self.cfg) as snapshot:
                self.assertIn("raw", snapshot.report["jobs"])

    def test_invalidated_epoch_cannot_publish(self):
        self.publish("raw", 1)
        original = (Path(self.cfg["manifest_root"]) / "latest").resolve()
        def reject():
            raise RuntimeError("epoch was invalidated before repair")
        with self.assertRaisesRegex(RuntimeError, "invalidated"):
            evidence.publish_family(self.cfg, "raw", self.values("raw", 2), self.artifacts("raw", 2), validate_epoch=reject)
        self.assertEqual((Path(self.cfg["manifest_root"]) / "latest").resolve(), original)

    def test_same_published_epoch_is_idempotent_after_queue_crash(self):
        values = self.values("raw", 1)
        stage = self.artifacts("raw", 1)
        first, _ = evidence.publish_family(self.cfg, "raw", values, stage)
        second, state = evidence.publish_family(self.cfg, "raw", values, stage)
        self.assertEqual(first, second)
        self.assertEqual(state["families"]["raw"]["clean_streak"], 1)

    def test_pin_survives_publication_and_history_cleanup(self):
        self.publish("raw", 1)
        with evidence.read_snapshot(self.cfg) as pinned:
            old = pinned.path
            for number in (2, 3, 4):
                self.publish("raw", number)
            evidence.cleanup_generations(self.cfg)
            self.assertTrue(old.is_dir())
            self.assertIn("raw-1", (old / "raw-local.tsv").read_text())
            self.assertEqual(pinned.gate["report_sha256"], hashlib.sha256((old / "comparison.json").read_bytes()).hexdigest())
        removed = evidence.cleanup_generations(self.cfg)
        self.assertIn(old.name, removed)
        self.assertFalse(old.exists())

    def test_cleanup_removes_only_old_abandoned_stages_without_following_links(self):
        self.publish("raw", 1)
        root = Path(self.cfg["manifest_root"]) / "generations"
        orphan, recent = root / ".incoming-orphan00", root / ".incoming-recent00"
        orphan.mkdir()
        (orphan / "partial.tsv").write_text("unfinished")
        recent.mkdir()
        old = time.time() - 16 * 3600
        os.utime(orphan, (old, old))
        target = self.root / "unrelated"
        target.mkdir()
        link = root / ".incoming-symlink0"
        link.symlink_to(target, target_is_directory=True)
        removed = evidence.cleanup_generations(self.cfg)
        self.assertIn(orphan.name, removed)
        self.assertFalse(orphan.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.is_dir())

    def test_snapshot_rejects_a_corrupt_gate_hash(self):
        self.publish("raw", 1)
        path = (Path(self.cfg["manifest_root"]) / "latest").resolve()
        gate_path = path / "verification-gate.json"
        value = json.loads(gate_path.read_text())
        value["report_sha256"] = "corrupt"
        gate_path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "pinned inventory"):
            with evidence.read_snapshot(self.cfg):
                pass

    def test_missing_artifact_cannot_replace_canonical_evidence(self):
        self.publish("raw", 1)
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, "artifact missing"):
            evidence.publish_family(self.cfg, "raw", self.values("raw", 2), empty)
        with evidence.read_snapshot(self.cfg) as snapshot:
            self.assertEqual(snapshot.report["jobs"]["raw"]["verification_id"], "raw-1")

    def test_publication_disk_reservation_preserves_canonical_pointer(self):
        self.publish("raw", 1)
        root = Path(self.cfg["manifest_root"])
        original = (root / "latest").resolve()
        self.cfg["recovery_free_reserve_bytes"] = 50 * 1024**3
        free = self.cfg["recovery_free_reserve_bytes"] + 512 * 1024
        with mock.patch.object(evidence.shutil, "disk_usage", return_value=SimpleNamespace(free=free)):
            with self.assertRaisesRegex(RuntimeError, "Canonical evidence publication"):
                self.publish("raw", 2)
            with self.assertRaisesRegex(RuntimeError, "Canonical evidence publication"):
                evidence.refresh_gate(self.cfg)
        self.assertEqual((root / "latest").resolve(), original)
        self.assertFalse(list((root / "generations").glob(".incoming-*")))

    def test_cache_quota_stops_publication_before_staging(self):
        self.publish("raw", 1)
        original = (Path(self.cfg["manifest_root"]) / "latest").resolve()
        self.cfg["recovery_cache_max_bytes"] = 1
        recovery = Path(self.cfg["manifest_root"]) / "recovery"
        (recovery / "full-cache").write_bytes(b"too large")
        with self.assertRaisesRegex(RuntimeError, "quota exhausted"):
            self.publish("raw", 2)
        self.assertEqual((Path(self.cfg["manifest_root"]) / "latest").resolve(), original)

    def test_external_gate_permissions_do_not_undo_canonical_publish(self):
        with mock.patch.object(evidence, "_compatibility_gate", side_effect=PermissionError("root owned")):
            _, gate = self.publish("raw", 1)
        with evidence.read_snapshot(self.cfg) as snapshot:
            self.assertEqual(snapshot.gate, gate)

    def test_refresh_keeps_confirmation_count_without_new_report(self):
        self.publish("raw", 1)
        self.publish("products", 1)
        before = (Path(self.cfg["manifest_root"]) / "latest").resolve()
        state = evidence.refresh_gate(self.cfg)
        after = (Path(self.cfg["manifest_root"]) / "latest").resolve()
        self.assertNotEqual(before, after)
        self.assertEqual(state["clean_streak"], 1)
        self.assertEqual((before / "comparison.json").read_bytes(), (after / "comparison.json").read_bytes())

    def legacy(self):
        root = Path(self.cfg["manifest_root"])
        latest = root / "latest"
        latest.mkdir(parents=True)
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        report = {"generated_at": stamp, "jobs": {"raw": self.values("raw", 0)}}
        raw = json.dumps(report).encode()
        (latest / "comparison.json").write_bytes(raw)
        (latest / "raw-local.tsv").write_text("legacy source\n")
        (latest / "raw-s3.tsv").write_text("legacy destination\n")
        state = {"last_generated_at": stamp, "report_sha256": hashlib.sha256(raw).hexdigest(),
                 "policy_version": 6, "clean_streak": 99, "clean": True}
        evidence._compatibility_gate(self.cfg, state)
        return latest, state, raw

    def test_atomic_legacy_migration_preserves_rollback_report_and_gate(self):
        latest, old_gate, old_report = self.legacy()
        self.publish("raw", 1)
        self.assertTrue(latest.is_symlink())
        backups = list(latest.parent.glob("legacy-latest-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "comparison.json").read_bytes(), old_report)
        evidence.rollback_legacy(self.cfg, backups[0])
        self.assertFalse(latest.is_symlink())
        self.assertEqual((latest / "comparison.json").read_bytes(), old_report)
        self.assertEqual(json.loads(Path(self.cfg["gate_state_path"]).read_text()), old_gate)
        with evidence.read_snapshot(self.cfg) as snapshot:
            self.assertEqual(snapshot.gate, old_gate)

    def test_failed_atomic_exchange_leaves_legacy_canonical_intact(self):
        latest, _, old_report = self.legacy()
        with mock.patch.object(evidence, "_exchange", side_effect=OSError("unsupported filesystem")):
            with self.assertRaisesRegex(OSError, "unsupported"):
                self.publish("raw", 1)
        self.assertFalse(latest.is_symlink())
        self.assertEqual((latest / "comparison.json").read_bytes(), old_report)


if __name__ == "__main__":
    unittest.main()
