from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_assets import verify

PLUGIN = Path(__file__).resolve().parents[1] / "hermes-workflows-py"


class AssetIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "plugin"
        for directory in ("dashboard", "desktop"):
            shutil.copytree(PLUGIN / directory, self.root / directory)

    def manifest(self, mutate):
        path = self.root / "desktop/assets.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data["files"])
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_committed_assets_are_valid(self):
        self.assertEqual(verify(self.root), 8)

    def test_corruption_is_detected(self):
        with (self.root / "dashboard/compat/index.js").open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            verify(self.root)

    def test_removed_manifest_record_cannot_hide_an_asset(self):
        self.manifest(lambda files: files.pop("plugin.js"))
        with self.assertRaisesRegex(ValueError, "missing required"):
            verify(self.root)

    def test_path_traversal_is_rejected(self):
        self.manifest(lambda files: files.update({"../../outside": "0" * 64}))
        with self.assertRaisesRegex(ValueError, "unsafe asset path"):
            verify(self.root)

    def test_oversized_bundle_is_rejected_even_with_updated_hash(self):
        payload = b"x" * (512 * 1024 + 1)
        (self.root / "desktop/plugin.js").write_bytes(payload)
        self.manifest(
            lambda files: files.update({"plugin.js": hashlib.sha256(payload).hexdigest()})
        )
        with self.assertRaisesRegex(ValueError, "512 KiB"):
            verify(self.root)
