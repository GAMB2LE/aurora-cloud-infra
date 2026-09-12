"""Active HATPRO root paths are not legacy archive duplicates."""
import copy
import csv
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


TEMPLATE = Path(__file__).parents[1] / "roles/gws_sync/templates/aurora-mirror-verify.py.j2"
FLAT = "HATPROG5-AURORA-ICELAND_260912_120000.LWP.NC"
DATED = "Y2026/M09/D12/" + FLAT
LEGACY = "HATPROG5-AURORA-ICELAND_260901_000000.LWP.NC"
NOW = dt.datetime(2026, 9, 12, 12, 0, tzinfo=dt.timezone.utc)


def entry(path, *, age=3600, size=100, checksum=""):
    return {"relpath": path, "size": size,
            "mtime": int(NOW.timestamp()) - age, "checksum": checksum}


def namespace(config):
    source = TEMPLATE.read_text()
    before, body = source.split("CONFIG = json.loads(", 1)
    _, after = body.split("\n\n\ndef json_default", 1)
    rendered = before + "CONFIG = " + repr(config) + "\n\n\ndef json_default" + after
    ns = {"__name__": "mirror_verify_test"}
    exec(compile(rendered, str(TEMPLATE), "exec"), ns)
    return ns


class ActivePathVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        key = self.root / "key"
        key.touch()
        self.stream = dict(name="hatprog5", source_user="instrument", source_host="source",
                           source_root="/instrument/raw", source_auth="tailscale", recursive=True,
                           source_pattern="HATPROG5-AURORA-ICELAND_*", local_root=str(self.root / "local"),
                           gws_relpath="hatprog5", ignore_flat_legacy=True,
                           product_patterns=["*.LWP.NC"], required_services=["append.service"])
        self.config = dict(manifest_root=str(self.root / "manifests"), history_keep=48,
                           checksum_mode="none", local_settle_seconds=600, gws_settle_seconds=2700,
                           product_settle_seconds=900, aurora_user="test", gws_user="archive",
                           gws_hosts=["gws-a", "gws-b"], gws_key=str(key), gws_raw_root="/gws/raw",
                           streams=[self.stream])
        self.ns = namespace(self.config)

    def run_snapshot(self, source, local, gws):
        def remote(base, target, root, recursive, pattern):
            return copy.deepcopy(source if target == "instrument@source" else gws)

        with mock.patch.dict(self.ns, {
            "utcnow": lambda: NOW,
            "list_remote_entries": mock.Mock(side_effect=remote),
            "list_local_entries": mock.Mock(return_value=copy.deepcopy(local)),
            "service_info": lambda name: dict(service=name, result="success", exit_timestamp=NOW),
        }):
            self.assertEqual(self.ns["main"](), 0)
        latest = self.root / "manifests/latest"
        summary = json.loads((latest / "summary.json").read_bytes())["streams"]["hatprog5"]
        manifests = {}
        for side in ("source", "local", "gws", "prune_candidates"):
            with (latest / "hatprog5" / (side + ".tsv")).open(newline="") as handle:
                manifests[side] = {r["relpath"]: r for r in csv.DictReader(handle, delimiter="\t")}
        return summary, manifests

    def test_all_36_delivered_active_flat_paths_survive_both_archive_inventories(self):
        paths = {FLAT + str(i): entry(FLAT + str(i)) for i in range(36)}
        archives = {**paths, LEGACY: entry(LEGACY), DATED: entry(DATED)}
        summary, manifests = self.run_snapshot(paths, archives, archives)
        for key in ("local_missing_count", "local_mismatch_count", "gws_missing_count", "gws_mismatch_count"):
            self.assertEqual(summary[key], 0)
        for side in ("local", "gws"):
            self.assertEqual(set(manifests[side]), set(paths) | {DATED})
        self.assertEqual(set(manifests["prune_candidates"]), set(paths))

    def test_previous_flat_filter_reproduces_36_false_gaps_on_both_destinations(self):
        paths = {FLAT + str(i): entry(FLAT + str(i)) for i in range(36)}
        previous_filter = lambda stream, entries, source: {p: r for p, r in entries.items() if "/" in p}
        with mock.patch.dict(self.ns, {"normalize_entries": previous_filter}):
            summary, _ = self.run_snapshot(paths, paths, paths)
        self.assertEqual(summary["local_missing_count"], 36)
        self.assertEqual(summary["gws_missing_count"], 36)

    def test_gws_fallback_keeps_the_same_required_paths(self):
        paths = {FLAT: entry(FLAT), LEGACY: entry(LEGACY)}
        listing = mock.Mock(side_effect=[RuntimeError("gateway unavailable"), paths])
        with mock.patch.dict(self.ns, {"list_remote_entries": listing}):
            entries, target, host = self.ns["list_gws_entries"](self.stream, {FLAT: entry(FLAT)})
        self.assertEqual(set(entries), {FLAT})
        self.assertEqual((target, host), ("archive@gws-b", "gws-b"))
        self.assertEqual(listing.call_count, 2)

    def test_gws_failure_does_not_become_empty_clean_evidence(self):
        with mock.patch.dict(self.ns, {"list_remote_entries": mock.Mock(side_effect=RuntimeError("gateway"))}):
            with self.assertRaisesRegex(RuntimeError, "all GWS transfer hosts failed"):
                self.ns["list_gws_entries"](self.stream, {FLAT: entry(FLAT)})

    def test_source_move_requires_exact_dated_paths_not_matching_flat_basename(self):
        source = {DATED: entry(DATED)}
        flat = {FLAT: entry(FLAT)}
        summary, manifests = self.run_snapshot(source, flat, flat)
        self.assertEqual(summary["local_missing_count"], 1)
        self.assertEqual(summary["gws_missing_count"], 1)
        self.assertFalse(manifests["prune_candidates"])
        summary, manifests = self.run_snapshot(source, {**source, **flat}, {**source, **flat})
        self.assertEqual(summary["local_missing_count"], 0)
        self.assertEqual(summary["gws_missing_count"], 0)
        self.assertEqual(set(manifests["local"]), {DATED})
        self.assertEqual(set(manifests["gws"]), {DATED})

    def test_real_flat_missing_and_mismatched_copies_remain_visible(self):
        source = {FLAT: entry(FLAT)}
        for side in ("local", "gws"):
            for bad, expected in (({}, "missing"), ({FLAT: entry(FLAT, size=99)}, "mismatch"),
                                  ({FLAT: entry(FLAT, age=3601)}, "mismatch")):
                with self.subTest(side=side, expected=expected):
                    local, gws = (bad, source) if side == "local" else (source, bad)
                    summary, manifests = self.run_snapshot(source, local, gws)
                    self.assertEqual(summary[side + "_" + expected + "_count"], 1)
                    self.assertFalse(manifests["prune_candidates"])

    def test_active_flat_checksum_mismatch_is_not_hidden(self):
        source = {FLAT: entry(FLAT, checksum="source")}
        bad = {FLAT: entry(FLAT, checksum="different")}
        summary, manifests = self.run_snapshot(source, bad, bad)
        self.assertEqual(summary["local_mismatch_count"], 1)
        self.assertEqual(summary["gws_mismatch_count"], 1)
        self.assertFalse(manifests["prune_candidates"])

    def test_local_and_gws_grace_periods_and_retention_age_stay_independent(self):
        for age, local_missing, gws_missing, retention_missing in (
            (599, 0, 0, 0), (600, 1, 0, 0), (2699, 1, 0, 0),
            (2700, 1, 1, 0), (7 * 86400, 1, 1, 0), (7 * 86400 + 1, 1, 1, 1),
        ):
            with self.subTest(age=age):
                summary, manifests = self.run_snapshot({FLAT: entry(FLAT, age=age)}, {}, {})
                self.assertEqual(summary["local_missing_count"], local_missing)
                self.assertEqual(summary["gws_missing_count"], gws_missing)
                self.assertEqual(summary["retention_local_missing_count"], retention_missing)
                self.assertEqual(summary["retention_gws_missing_count"], retention_missing)
                self.assertFalse(manifests["prune_candidates"])

    def test_non_hatpro_inventory_is_unchanged_and_source_is_not_filtered_or_mutated(self):
        source = {FLAT: entry(FLAT)}
        archive = {**source, LEGACY: entry(LEGACY), DATED: entry(DATED)}
        before = copy.deepcopy((source, archive))
        self.assertIs(self.ns["normalize_entries"]({}, archive, source), archive)
        self.assertEqual(set(self.ns["normalize_entries"](self.stream, archive, {})), {DATED})
        self.assertEqual((source, archive), before)


if __name__ == "__main__":
    unittest.main()
