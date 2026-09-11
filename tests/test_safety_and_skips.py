"""Tests for the 1.2.1 fixes: path policy, skipped-file reporting, --no-index.

These cover three defects found by running the tool on a real heterogeneous tree:

* ``/etc``-style names were rejected anywhere in a path, so any project with a
  ``bin/`` directory silently lost files.
* Files skipped by that policy (and symlinks) vanished without a word: a
  556-file tree produced a 356-file archive with no signal.
* A single symlink inside a directory aborted the whole directory.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from core import packer
from core.errors import SafetyError
from core.format import read_manifest
from core.packer import analyze, pack, search_archive, unpack
from core.safety import ensure_safe_input


class PathPolicyTests(unittest.TestCase):
    def test_user_tree_with_bin_directory_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "project" / "bin" / "cli.js"
            target.parent.mkdir(parents=True)
            target.write_text("#!/usr/bin/env node\n", encoding="utf-8")

            self.assertEqual(ensure_safe_input(target), target.resolve())

    def test_user_tree_with_var_and_etc_directories_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("var", "etc", "sbin"):
                target = root / "app" / name / "data.txt"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("payload", encoding="utf-8")
                self.assertEqual(ensure_safe_input(target), target.resolve())

    @unittest.skipIf(os.name == "nt", "POSIX system paths only")
    def test_leading_system_directories_are_still_rejected(self):
        for path in ("/etc/passwd", "/usr/bin/env", "/var/log/syslog", "/proc/self"):
            with self.subTest(path=path):
                with self.assertRaises(SafetyError):
                    ensure_safe_input(Path(path))

    def test_protected_names_are_still_rejected_anywhere(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "repo" / ".git" / "config"
            target.parent.mkdir(parents=True)
            target.write_text("[core]", encoding="utf-8")
            with self.assertRaises(SafetyError):
                ensure_safe_input(target)


class SkippedFileReportingTests(unittest.TestCase):
    def _tree(self, root: Path) -> Path:
        source = root / "project"
        (source / "bin").mkdir(parents=True)
        (source / "bin" / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
        (source / "README.md").write_text("# Project\n", encoding="utf-8")
        (source / ".git").mkdir()
        (source / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        (source / "__pycache__").mkdir()
        (source / "__pycache__" / "mod.pyc").write_bytes(b"\x00\x01")
        return source

    def test_bin_directory_files_are_packed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._tree(root)
            archive = root / "a.vibo"

            result = pack([source], archive)

            paths = {entry["path"] for entry in read_manifest(archive)["entries"]}
            self.assertIn("project/bin/cli.js", paths)
            self.assertEqual(result["skipped_by_reason"], {"protected": 2})

    def test_skipped_files_are_reported_with_reasons(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._tree(root)

            result = pack([source], root / "a.vibo")

            self.assertEqual(result["skipped_count"], 2)
            self.assertEqual(result["skipped_by_reason"], {"protected": 2})
            reasons = {item["reason"] for item in result["skipped"]}
            self.assertEqual(reasons, {"protected"})
            self.assertTrue(
                all(item["path"].startswith("project/") for item in result["skipped"])
            )

    def test_analyze_reports_skipped_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._tree(root)

            plan = analyze([source])

            self.assertEqual(plan["skipped_summary"]["count"], 2)
            self.assertEqual(plan["skipped_summary"]["by_reason"], {"protected": 2})

    def test_empty_report_when_nothing_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "clean"
            source.mkdir()
            (source / "note.md").write_text("hello", encoding="utf-8")

            result = pack([source], root / "a.vibo")

            self.assertEqual(result["skipped_count"], 0)
            self.assertEqual(result["skipped"], [])
            self.assertEqual(result["skipped_by_reason"], {})


class SymlinkHandlingTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlink_inside_directory_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "project"
            source.mkdir()
            (source / "real.txt").write_text("payload", encoding="utf-8")
            os.symlink(source / "real.txt", source / "link.txt")
            archive = root / "a.vibo"

            result = pack([source], archive)

            self.assertEqual(result["entry_count"], 1)
            self.assertEqual(result["skipped_by_reason"], {"symlink": 1})
            self.assertEqual(result["skipped"][0]["path"], "project/link.txt")
            restored = root / "out"
            unpack(archive, restored)
            self.assertTrue((restored / "project" / "real.txt").exists())

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_explicit_symlink_input_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "real.txt"
            target.write_text("payload", encoding="utf-8")
            link = root / "link.txt"
            os.symlink(target, link)

            with self.assertRaises(SafetyError):
                pack([link], root / "a.vibo")


class VanishedFileTests(unittest.TestCase):
    """Files that disappear mid-run must be reported, not abort the archive.

    Found on a real tree: SQLite removes its ``-shm`` sibling while a pack was
    running, and the whole 2.2 GB backup died with FileNotFoundError.
    """

    def test_file_that_vanishes_mid_run_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            source.mkdir()
            (source / "keep.md").write_text("keep", encoding="utf-8")
            (source / "gone.md").write_text("gone", encoding="utf-8")
            archive = root / "a.vibo"
            real_spool = packer._spool_source

            def flaky(path, *args, **kwargs):
                if Path(path).name == "gone.md":
                    raise FileNotFoundError(str(path))
                return real_spool(path, *args, **kwargs)

            with patch("core.packer._spool_source", side_effect=flaky):
                result = pack([source], archive)

            self.assertEqual(result["entry_count"], 1)
            self.assertEqual(result["skipped_by_reason"], {"vanished": 1})
            self.assertEqual(result["skipped"][0]["path"], "tree/gone.md")

    def test_missing_file_is_reported_in_analyze(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            source.mkdir()
            (source / "keep.md").write_text("keep", encoding="utf-8")
            (source / "gone.md").write_text("gone", encoding="utf-8")
            original = packer._stream_estimate

            def flaky(*args, **kwargs):
                raise FileNotFoundError("gone")

            with patch("core.packer._stream_estimate", side_effect=flaky):
                plan = analyze([source])

            self.assertEqual(plan["skipped_summary"]["by_reason"], {"vanished": 2})


class NoIndexTests(unittest.TestCase):
    def test_no_index_archives_without_lexical_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "docs"
            source.mkdir()
            (source / "note.md").write_text(
                "CloudArc bounded memory notes.", encoding="utf-8"
            )
            archive = root / "a.vibo"

            result = pack([source], archive, index=False)

            # result["index"] must keep pointing at the index sidecar file
            self.assertTrue(str(result["index"]).endswith(".vibo.index.json"))
            self.assertTrue(Path(result["index"]).exists())
            self.assertFalse(result["index_built"])
            self.assertFalse(result["search"]["index_built"])
            manifest = read_manifest(archive)
            self.assertFalse(manifest["search"]["index_built"])
            # The format contract is unchanged: the default mode stays lexical.
            self.assertEqual(manifest["search"]["default_mode"], "lexical")
            self.assertEqual(search_archive(archive, "cloudarc"), [])

            restored = root / "out"
            unpack(archive, restored)
            self.assertEqual(
                (restored / "docs" / "note.md").read_text(encoding="utf-8"),
                "CloudArc bounded memory notes.",
            )

    def test_index_is_built_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "docs"
            source.mkdir()
            (source / "note.md").write_text("CloudArc bounded memory.", encoding="utf-8")

            result = pack([source], root / "a.vibo")

            self.assertTrue(result["index_built"])
            self.assertTrue(result["search"]["index_built"])


if __name__ == "__main__":
    unittest.main()
