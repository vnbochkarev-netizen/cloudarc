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
from core.errors import FormatError, SafetyError
from core.manifest import validate_manifest
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
    def test_symlink_inside_directory_is_stored_not_followed(self):
        """A symlink inside a tree is kept as a link entry (1.4.0 behaviour).

        Before 1.4.0 it was skipped, so a restore silently lost the link. Now the
        link target is stored as the entry payload and the link is recreated —
        without ever being followed while packing.
        """

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "project"
            source.mkdir()
            (source / "real.txt").write_text("payload", encoding="utf-8")
            os.symlink("real.txt", source / "link.txt")
            archive = root / "a.vibo"

            result = pack([source], archive)

            self.assertEqual(result["entry_count"], 3)
            self.assertEqual(result["kinds"], {"file": 1, "symlink": 1, "dir": 1})
            self.assertEqual(result["skipped_by_reason"], {})
            manifest = read_manifest(archive)
            links = [
                entry for entry in manifest["entries"] if entry["kind"] == "symlink"
            ]
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0]["target"], "real.txt")

            restored = root / "out"
            unpack(archive, restored)
            restored_link = restored / "project" / "link.txt"
            self.assertTrue(restored_link.is_symlink())
            self.assertEqual(os.readlink(restored_link), "real.txt")
            self.assertEqual(
                (restored / "project" / "real.txt").read_text(encoding="utf-8"),
                "payload",
            )

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlinks_are_skipped_and_reported_without_metadata(self):
        """``metadata=False`` keeps the pre-1.4.0 contract for callers that want it."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "project"
            source.mkdir()
            (source / "real.txt").write_text("payload", encoding="utf-8")
            os.symlink("real.txt", source / "link.txt")
            archive = root / "a.vibo"

            result = pack([source], archive, metadata=False)

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

            self.assertEqual(result["entry_count"], 2)
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

            # The tree root is an entry as well, so all three payloads vanish.
            self.assertEqual(plan["skipped_summary"]["by_reason"], {"vanished": 3})


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


class ChangingFileTests(unittest.TestCase):
    """Files that are still being written must not kill the whole archive.

    Found on a live server: packing ``/var/log`` aborted with
    ``source changed while packing: /var/log/syslog`` - one growing log file
    destroyed a 5 GB backup. A backup tool must report such a file, not abort.
    """

    def _tree(self, root: Path) -> tuple[Path, Path]:
        source = root / "logs"
        source.mkdir()
        (source / "stable.log").write_text("quiet\n" * 10, encoding="utf-8")
        busy = source / "busy.log"
        busy.write_text("growing\n" * 10, encoding="utf-8")
        return source, busy

    def _growing_open(self, busy: Path):
        """Append to ``busy`` the moment it is opened for reading.

        Deterministic stand-in for a live log file: the file is a different size
        after the read than it was before it, which is exactly what the spooler
        detects with its before/after stat pair.
        """

        real_open = Path.open

        def fake_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            if self == busy and args and "r" in str(args[0]):
                with real_open(busy, "ab") as extra:
                    extra.write(b"more\n")
            return handle

        return fake_open

    def test_a_file_being_written_is_skipped_and_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, busy = self._tree(root)
            archive = root / "a.vibo"

            # workers=1: the pool would run the spooler in another process where
            # this patched ``open`` does not exist.
            with patch.object(Path, "open", self._growing_open(busy)):
                result = pack([source], archive, workers=1)

            self.assertEqual(result["changing_count"], 0)
            self.assertEqual(result["skipped_by_reason"], {"changed": 1})
            self.assertEqual(result["skipped"][0]["path"], "logs/busy.log")
            self.assertTrue(archive.exists())
            manifest = read_manifest(archive)
            paths = {entry["path"] for entry in manifest["entries"]}
            self.assertIn("logs/stable.log", paths)
            self.assertNotIn("logs/busy.log", paths)

    def test_accept_changing_stores_a_torn_snapshot_and_marks_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, busy = self._tree(root)
            archive = root / "a.vibo"

            with patch.object(Path, "open", self._growing_open(busy)):
                result = pack(
                    [source], archive, accept_changing=True, workers=1
                )

            self.assertEqual(result["changing_count"], 1)
            self.assertEqual(result["accept_changing"], True)
            self.assertTrue(
                any("torn snapshot" in warning for warning in result["warnings"]),
                result["warnings"],
            )
            manifest = read_manifest(archive)
            marked = [
                entry for entry in manifest["entries"] if entry.get("changing")
            ]
            self.assertEqual([entry["path"] for entry in marked], ["logs/busy.log"])

            restored = root / "out"
            unpack(archive, restored)
            # A torn snapshot: the file is there and starts with the original
            # bytes; the bytes appended during the read may or may not be inside.
            self.assertTrue(
                (restored / "logs" / "busy.log")
                .read_text(encoding="utf-8")
                .startswith("growing\n")
            )

    def test_changing_flag_must_be_a_boolean(self):
        manifest = {
            "schema": "cloudarc.manifest",
            "schema_version": 1,
            "archive_id": "00000000-0000-0000-0000-000000000000",
            "created_at": "2026-09-11T00:00:00+00:00",
            "tool": "cloudarc",
            "tool_version": "1.4.0",
            "entry_count": 1,
            "raw_bytes": 1,
            "entries": [
                {
                    "entry_id": "e00000000",
                    "path": "logs/busy.log",
                    "kind": "file",
                    "size": 1,
                    "sha256": "a" * 64,
                    "chunk_id": "c0",
                    "chunk_offset": 0,
                    "chunk_length": 1,
                    "stored_size": 1,
                    "codec": "store",
                    "searchable": False,
                    "changing": "yes",
                }
            ],
            "methods": {"store": 1},
        }
        with self.assertRaises(FormatError):
            validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
