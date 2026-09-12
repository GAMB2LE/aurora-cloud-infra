"""The focused verifier rollout preserves bindings, retention and live workers."""
import ast
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("render_mirror_patch", ROOT / "scripts/render_mirror_verifier_patch.py")
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


class MirrorVerifierDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.template = (ROOT / "roles/gws_sync/templates/aurora-mirror-verify.py.j2").read_text()
        self.config = dict(local_settle_seconds=600, gws_settle_seconds=2700,
                           retention_days=7, checksum_mode="full", source_root="/unchanged/source",
                           streams=[dict(name="hatprog5", ignore_flat_legacy=True)])
        before, body = self.template.split("CONFIG = json.loads(", 1)
        _, after = body.split("\n\n\ndef json_default", 1)
        self.assignment = "CONFIG = json.loads(\n    r'''\n" + json.dumps(self.config, indent=2) + "\n'''\n)"
        self.deployed = before + self.assignment + "\n\n\ndef json_default" + after

    def test_exact_deployed_configuration_is_preserved_not_rerendered_from_inventory(self):
        candidate = patch.render(self.deployed, self.template)
        self.assertEqual(candidate, self.deployed)
        tree = ast.parse(candidate)
        config = next(n for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "CONFIG" for t in n.targets))
        self.assertEqual(ast.get_source_segment(candidate, config), self.assignment)

    def test_only_three_audited_functions_can_change(self):
        old = self.deployed.replace('if "/" in relpath or relpath in source_entries', 'if "/" in relpath')
        candidate = patch.render(old, self.template)
        self.assertEqual(candidate, self.deployed)
        for change in (
            self.template.replace('def compare_entries(', 'def changed_compare_entries('),
            self.template.replace('int(entry["mtime"]) <= cutoff', 'int(entry["mtime"]) < cutoff'),
            self.template + '\nraise RuntimeError("unrelated top-level change")\n',
        ):
            with self.subTest(change=change[-80:]):
                self.assertNotEqual(change, self.template)
                with self.assertRaisesRegex(ValueError, "outside the three"):
                    patch.render(self.deployed, change)

    def test_invalid_or_executable_configuration_is_rejected_without_execution(self):
        for assignment in (
            "CONFIG = json.loads(get_runtime_config())",
            "CONFIG = json.loads('{}', object_hook=run_code)",
            "CONFIG = load_config()",
            "CONFIG = json.loads('[]')",
            self.assignment + "\nCONFIG = json.loads('{}')",
        ):
            with self.subTest(assignment=assignment[:70]):
                with self.assertRaises((ValueError, TypeError)):
                    patch.render(self.deployed.replace(self.assignment, assignment), self.template)

    def test_duplicate_or_missing_patch_functions_are_rejected(self):
        for source in (self.deployed + "\ndef main(): pass\n",
                       self.deployed.replace("def normalize_entries(", "def unknown_entries(")):
            with self.subTest(source=source[-60:]):
                with self.assertRaises(ValueError):
                    patch.render(source, self.template)

    def test_playbook_is_reader_only_with_verified_rollback_and_acceptance_reset(self):
        text = (ROOT / "playbooks/mirror_verifier_active_paths.yml").read_text()
        for forbidden in ("ansible.builtin.systemd", "ansible.builtin.service", "ansible.builtin.shell",
                          "notify:", "dest: /etc/", "queue.retry(", "queue.enqueue(", "queue.invalidate("):
            self.assertNotIn(forbidden, text)
        self.assertIn("queue.record_manual_intervention()", text)
        self.assertIn("mirror_verifier_installed.changed", text)
        self.assertIn("not ansible_check_mode", text)
        self.assertIn("unsafe_writes: false", text)
        self.assertIn("diff: false", text)
        self.assertIn("mirror_verifier_saved.stat.checksum == mirror_verifier_candidate.before_sha256", text)
        self.assertIn("not mirror_verifier_before.results[3].stat.exists", text)
        self.assertLess(text.index("Verify saved bytes"), text.index("Atomically install"))
        self.assertLess(text.index("Recheck that the independent verifier remains idle"), text.index("Atomically install"))
        self.assertLess(text.index("Atomically install"), text.index("queue.record_manual_intervention()"))


if __name__ == "__main__":
    unittest.main()
