from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


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
