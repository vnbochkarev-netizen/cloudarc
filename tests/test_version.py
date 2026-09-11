"""One number, one place: the version must not drift between code, CLI and manifest.

1.2.1 shipped with ``cloudarc.py version`` reporting ``0.1.0-mvp`` while
``pyproject.toml`` said ``1.2.1`` - the same stale string the manifest carried in
1.2.0. These tests make that class of drift a build failure.
"""

from __future__ import annotations

import json
import pathlib
import re
import tempfile
import unittest
from pathlib import Path

from core.packer import pack
from core.version import VERSION, package_version
from core.format import read_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]


class VersionConsistencyTests(unittest.TestCase):
    def test_version_is_not_the_stale_mvp_string(self):
        self.assertNotIn("mvp", VERSION.lower())

    def test_pyproject_declares_the_same_version(self):
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
        self.assertIsNotNone(match, "pyproject.toml has no version")
        self.assertEqual(match.group(1), VERSION)

    def test_cli_module_reports_the_package_version(self):
        import cloudarc

        self.assertEqual(cloudarc.VERSION, VERSION)

    def test_manifest_records_the_package_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "docs"
            source.mkdir()
            (source / "note.md").write_text("version check", encoding="utf-8")
            archive = root / "a.vibo"

            pack([source], archive)

            manifest = read_manifest(archive)
            self.assertEqual(manifest["tool_version"], VERSION)

    def test_packaged_skill_version_matches(self):
        version_file = REPO_ROOT / "skills" / "cloudarc-bounded-memory-benchmark" / "VERSION"
        if not version_file.exists():
            self.skipTest("skill package not present")
        self.assertEqual(version_file.read_text(encoding="utf-8").strip(), VERSION)


if __name__ == "__main__":
    unittest.main()
