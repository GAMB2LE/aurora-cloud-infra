"""GWS product access survives restrictive publishers and atomic replacements."""
import grp
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((ROOT / "inventory/group_vars/aurora_cloud.yml").read_text())
TEMPLATE = (ROOT / "roles/gws_sync/templates/aurora-gws-rsync.sh.j2").read_text()
GNU_RSYNC = bool(shutil.which("rsync")) and subprocess.run(
    ["rsync", "--version"], capture_output=True, text=True, check=True,
).stdout.startswith("rsync  version")


def render(job, **overrides):
    renderer = Environment(undefined=StrictUndefined)
    renderer.filters["quote"] = shlex.quote
    return renderer.from_string(TEMPLATE).render(**dict(CONFIG, **overrides), item=job)


def options(script):
    array = re.search(r"^rsync_opts=\(.*?^\)", script, re.M | re.S).group()
    result = subprocess.run(
        ["bash"], input=array + '\nprintf "%s\\0" "${rsync_opts[@]}"\n',
        text=True, capture_output=True, check=True,
    )
    return result.stdout.rstrip("\0").split("\0")


class GwsProductPermissionTests(unittest.TestCase):
    def test_only_product_jobs_normalize_permissions_and_group(self):
        for job in CONFIG["gws_sync_jobs"]:
            with self.subTest(job=job["name"]):
                script = render(job)
                subprocess.run(["bash", "-n"], input=script, text=True, check=True)
                args = options(script)
                enabled = job["name"] in {"products", "products-wxcam"}
                self.assertEqual("--chmod=Dg+rwx,Dg+s,Fg+rw" in args, enabled)
                self.assertEqual("--chown=:gws_gamb2le" in args, enabled)
                self.assertEqual([arg for arg in args if arg.startswith("--exclude=")],
                                 ["--exclude=" + pattern for pattern in job.get("exclude", [])])
                self.assertFalse(any(arg.startswith("--delete") for arg in args))
                self.assertEqual("-L" in args, bool(job.get("copy_links")))

    def test_group_is_rendered_from_configuration(self):
        renderer = Environment(undefined=StrictUndefined)
        renderer.filters["quote"] = shlex.quote
        job = next(job for job in CONFIG["gws_sync_jobs"] if job["name"] == "products")
        script = renderer.from_string(TEMPLATE).render(
            **dict(CONFIG, gws_group="gws_fixture"), item=job,
        )
        self.assertIn("--chown=:gws_fixture", options(script))

    @unittest.skipUnless(GNU_RSYNC, "requires production GNU rsync; macOS openrsync ignores this chmod policy")
    def test_restrictive_files_and_replacements_remain_shared_without_deletion(self):
        job = next(job for job in CONFIG["gws_sync_jobs"] if job["name"] == "products")
        group = grp.getgrgid(os.getgid()).gr_name
        args = options(render(job, gws_group=group))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source", root / "destination"
            (source / "private").mkdir(parents=True, mode=0o700)
            destination.mkdir()
            archived = destination / "archive-only.txt"
            archived.write_bytes(b"keep archived data")
            data = source / "private" / "data.json"
            target = destination / "private" / "data.json"
            for content, timestamp in ((b"first", 1_700_000_000), (b"replacement", 1_700_000_010)):
                replacement = source / "private" / ".replacement"
                replacement.write_bytes(content)
                replacement.chmod(0o600)
                os.utime(replacement, (timestamp, timestamp))
                replacement.replace(data)
                subprocess.run(["rsync", *args, str(source) + "/", str(destination) + "/"],
                               check=True, capture_output=True)
                self.assertEqual(target.read_bytes(), content)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o660)
                self.assertEqual(target.stat().st_gid, os.getgid())
                self.assertEqual(int(target.stat().st_mtime), timestamp)
                self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o2770)
                self.assertEqual(archived.read_bytes(), b"keep archived data")
                self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
