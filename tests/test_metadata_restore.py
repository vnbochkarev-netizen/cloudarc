"""Metadata-preserving pack/unpack (1.4.0).

Before 1.4.0 a restore returned file *contents* only: permission bits were
dropped (640 -> 644), symlinks were skipped with a warning, and empty
directories disappeared. A backup that cannot rebuild the tree it came from is
not a restore, so 1.4.0 stores ``kind``/``mode``/``mtime_ns``/``target`` for
every entry and rebuilds them on unpack.

Found by packing a real tree and diffing the result: 375 of 3919 files
survived, every mode was 644 after the round trip, and the two symlinks in the
tree were reported as skipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.errors import FormatError, SafetyError
from core.format import read_header, read_index, read_manifest
from core.manifest import validate_manifest
from core.packer import pack, search_archive, unpack

POSIX_ONLY = unittest.skipIf(
    os.name == "nt", "POSIX permission bits and symlinks"
)
FIXED_MTIME_NS = 1_700_000_000_123_456_789


class ModeAndTimeRestoreTests(unittest.TestCase):
    @POSIX_ONLY
    def test_file_modes_survive_the_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            source.mkdir()
            private = source / "secret.key"
            private.write_text("shh", encoding="utf-8")
            private.chmod(0o600)
            script = source / "run.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            script.chmod(0o750)

            archive = root / "a.vibo"
            pack([source], archive)
            restored = root / "out"
            unpack(archive, restored)

            modes = {
                path.name: stat.S_IMODE(path.stat().st_mode)
                for path in (restored / "tree").iterdir()
            }
            self.assertEqual(modes["secret.key"], 0o600)
            self.assertEqual(modes["run.sh"], 0o750)

    @POSIX_ONLY
    def test_directory_mode_is_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            (source / "private").mkdir(parents=True)
            (source / "private" / "note.txt").write_text("x", encoding="utf-8")
            (source / "private").chmod(0o700)

            archive = root / "a.vibo"
            pack([source], archive)
            restored = root / "out"
            unpack(archive, restored)

            self.assertEqual(
                stat.S_IMODE((restored / "tree" / "private").stat().st_mode),
                0o700,
            )

    @POSIX_ONLY
    def test_modification_time_is_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            source.mkdir()
            note = source / "note.txt"
            note.write_text("x", encoding="utf-8")
            os.utime(note, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))

            archive = root / "a.vibo"
            pack([source], archive)
            restored = root / "out"
            unpack(archive, restored)

            self.assertEqual(
                (restored / "tree" / "note.txt").stat().st_mtime_ns,
                FIXED_MTIME_NS,
            )

    @POSIX_ONLY
    def test_restore_metadata_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            source.mkdir()
            private = source / "secret.key"
            private.write_text("shh", encoding="utf-8")
            private.chmod(0o600)

            archive = root / "a.vibo"
            pack([source], archive)
            restored = root / "out"
            unpack(archive, restored, restore_metadata=False)

            self.assertNotEqual(
                stat.S_IMODE((restored / "tree" / "secret.key").stat().st_mode),
                0o600,
            )

    @POSIX_ONLY
    def test_pack_without_metadata_records_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            source.mkdir()
            (source / "note.txt").write_text("x", encoding="utf-8")

            archive = root / "a.vibo"
            result = pack([source], archive, metadata=False)

            self.assertFalse(result["metadata"])
            for entry in read_manifest(archive)["entries"]:
                self.assertNotIn("mode", entry)
                self.assertNotIn("mtime_ns", entry)


class DirectoryEntryTests(unittest.TestCase):
    def test_empty_directory_is_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            (source / "empty").mkdir(parents=True)
            (source / "note.txt").write_text("x", encoding="utf-8")

            archive = root / "a.vibo"
            result = pack([source], archive)
            self.assertEqual(result["kinds"], {"file": 1, "symlink": 0, "dir": 2})

            restored = root / "out"
            unpack(archive, restored)
            self.assertTrue((restored / "tree" / "empty").is_dir())

    def test_nested_empty_directories_are_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            (source / "a" / "b" / "c").mkdir(parents=True)
            (source / "keep.txt").write_text("x", encoding="utf-8")

            archive = root / "a.vibo"
            pack([source], archive)
            restored = root / "out"
            unpack(archive, restored)

            self.assertTrue((restored / "tree" / "a" / "b" / "c").is_dir())

    def test_only_regular_files_enter_the_search_index(self):
        """Directory markers and link targets are metadata, not searchable content."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            (source / "empty-marker").mkdir(parents=True)
            (source / "note.txt").write_text("unique-word-here", encoding="utf-8")
            if os.name != "nt":
                os.symlink("unique-word-here", source / "link-with-words")

            archive = root / "a.vibo"
            pack([source], archive)

            manifest = read_manifest(archive)
            kinds = {entry["entry_id"]: entry.get("kind", "file") for entry in manifest["entries"]}
            index = read_index(archive)
            lexical = index.get("lexical", index)
            documents = lexical.get("documents", {})
            self.assertTrue(documents, "the index must contain the regular file")
            for entry_id in documents:
                self.assertEqual(kinds[entry_id], "file")
            self.assertEqual(search_archive(archive, "unique-word-here")[0]["path"], "tree/note.txt")


@POSIX_ONLY
class SymlinkRestoreTests(unittest.TestCase):
    def test_relative_and_broken_symlinks_are_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            source.mkdir()
            (source / "real.txt").write_text("payload", encoding="utf-8")
            os.symlink("real.txt", source / "link.txt")
            os.symlink("missing.txt", source / "broken.txt")

            archive = root / "a.vibo"
            pack([source], archive)
            restored = root / "out"
            unpack(archive, restored)

            restored_link = restored / "tree" / "link.txt"
            self.assertTrue(restored_link.is_symlink())
            self.assertEqual(os.readlink(restored_link), "real.txt")
            self.assertEqual(restored_link.read_text(encoding="utf-8"), "payload")
            broken = restored / "tree" / "broken.txt"
            self.assertTrue(broken.is_symlink())
            self.assertFalse(broken.exists())

    def test_absolute_symlink_target_is_stored_not_followed(self):
        """The link target string is stored — the target file is never read."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside.bin"
            outside.write_bytes(b"X" * 5_000_000)
            source = root / "tree"
            source.mkdir()
            (source / "note.txt").write_text("x", encoding="utf-8")
            os.symlink(outside, source / "huge-link")

            archive = root / "a.vibo"
            pack([source], archive)

            manifest = read_manifest(archive)
            link_entry = next(
                entry for entry in manifest["entries"] if entry["path"].endswith("huge-link")
            )
            self.assertEqual(link_entry["kind"], "symlink")
            self.assertEqual(link_entry["target"], str(outside))
            # 5 MB of file content must not have entered the archive.
            self.assertLess(link_entry["size"], 4096)
            self.assertLess(archive.stat().st_size, 1_000_000)

            restored = root / "out"
            unpack(archive, restored)
            self.assertEqual(os.readlink(restored / "tree" / "huge-link"), str(outside))

    def test_symlinked_directory_is_not_walked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside"
            outside.mkdir()
            (outside / "leak.txt").write_text("secret", encoding="utf-8")
            source = root / "tree"
            source.mkdir()
            (source / "note.txt").write_text("x", encoding="utf-8")
            os.symlink(outside, source / "linkdir")

            archive = root / "a.vibo"
            result = pack([source], archive)

            paths = {
                entry["path"] for entry in read_manifest(archive)["entries"]
            }
            self.assertEqual(paths, {"tree", "tree/note.txt", "tree/linkdir"})
            self.assertEqual(result["kinds"]["symlink"], 1)

    def test_explicit_symlink_input_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "real.txt"
            target.write_text("payload", encoding="utf-8")
            link = root / "link.txt"
            os.symlink(target, link)

            with self.assertRaises(SafetyError):
                pack([link], root / "a.vibo")


class SymlinkEscapeTests(unittest.TestCase):
    """An archive must not be able to write outside the output directory.

    The classic attack is "create a link, then write through it". CloudArc
    refuses such an archive at validation time and, as a second line of defence,
    creates every link only after the regular files.
    """

    def _forged_manifest(self, archive: Path, outside: Path) -> dict:
        manifest = read_manifest(archive)
        donor = dict(manifest["entries"][0])
        target = str(outside)
        link = {
            "entry_id": "e00000000",
            "path": "escape",
            "kind": "symlink",
            "target": target,
            "size": len(target),
            "sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
            "chunk_id": donor["chunk_id"],
            "chunk_offset": donor["chunk_offset"],
            "chunk_length": donor["chunk_length"],
            "stored_size": donor["chunk_length"],
            "codec": donor.get("codec", "store"),
            "searchable": False,
        }
        payload = {**donor, "path": "escape/evil.txt", "kind": "file"}
        forged = json.loads(json.dumps(manifest))
        forged["entries"] = [link, payload]
        forged["entry_count"] = 2
        return forged

    def test_manifest_rejects_entries_below_a_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            source.mkdir()
            (source / "note.txt").write_text("payload", encoding="utf-8")
            archive = root / "a.vibo"
            pack([source], archive)

            with self.assertRaises(FormatError):
                validate_manifest(self._forged_manifest(archive, root / "outside"))

    def test_unpack_never_writes_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            source.mkdir()
            (source / "note.txt").write_text("payload", encoding="utf-8")
            archive = root / "a.vibo"
            pack([source], archive)
            outside = root / "outside"
            outside.mkdir()
            forged = self._forged_manifest(archive, outside)
            out_dir = root / "out"

            # A real attacker would ship a *consistent* index; the point of this
            # test is the runtime guard, not the validation step (covered above).
            with patch("core.packer.read_manifest", return_value=forged), patch(
                "core.packer.validate_manifest_index_alignment"
            ):
                with self.assertRaises(SafetyError):
                    unpack(archive, out_dir)

            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((outside / "evil.txt").exists())


class FormatVersionTests(unittest.TestCase):
    """A 1.x engine must refuse a metadata archive instead of restoring it wrong.

    Version 1 readers ignore unknown manifest fields, so they would happily write
    an empty file for a directory marker and a text file containing the link
    target for a symlink. The container header therefore declares version 2, and
    a 1.x reader refuses it ("unsupported VIBO format version").
    """

    def test_archive_with_metadata_entries_declares_version_two(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "note.txt").write_text("x", encoding="utf-8")

            archive = root / "a.vibo"
            pack([source], archive)

            self.assertEqual(read_header(archive)["format_version"], 2)

    def test_a_single_file_input_stays_version_one(self):
        """Only a file input is metadata-free: a directory is always an entry."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            single = root / "note.txt"
            single.write_text("x", encoding="utf-8")

            archive = root / "a.vibo"
            pack([single], archive)

            self.assertEqual(read_header(archive)["format_version"], 1)

    def test_directory_input_declares_version_two(self):
        """A directory input is always an entry, so 1.x readers must refuse it."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            source.mkdir()
            (source / "note.txt").write_text("x", encoding="utf-8")

            archive = root / "a.vibo"
            pack([source], archive)

            self.assertEqual(read_header(archive)["format_version"], 2)

    def test_contents_only_archive_stays_version_one(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "note.txt").write_text("x", encoding="utf-8")

            archive = root / "a.vibo"
            pack([source], archive, metadata=False)

            self.assertEqual(read_header(archive)["format_version"], 1)

    def test_a_version_one_reader_refuses_a_version_two_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "note.txt").write_text("x", encoding="utf-8")
            archive = root / "a.vibo"
            pack([source], archive)

            with patch("core.format.SUPPORTED_FORMAT_VERSIONS", {1}):
                with self.assertRaises(FormatError):
                    read_header(archive)
                with self.assertRaises(FormatError):
                    unpack(archive, root / "out")

    def test_unknown_format_version_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            source.mkdir()
            (source / "note.txt").write_text("x", encoding="utf-8")
            archive = root / "a.vibo"
            pack([source], archive)

            with patch("core.format.SUPPORTED_FORMAT_VERSIONS", {7}):
                with self.assertRaises(FormatError):
                    read_header(archive)


class ManifestContractTests(unittest.TestCase):
    def _manifest(self, **entry_changes) -> dict:
        entry = {
            "entry_id": "e00000000",
            "path": "tree/note.txt",
            "kind": "file",
            "size": 3,
            "sha256": hashlib.sha256(b"abc").hexdigest(),
            "chunk_id": "c00000000-abcdef0123456789",
            "chunk_offset": 5,
            "chunk_length": 3,
            "stored_size": 3,
            "codec": "store",
            "searchable": False,
        }
        entry.update(entry_changes)
        return {
            "schema": "cloudarc.manifest",
            "schema_version": 1,
            "archive_id": "00000000-0000-0000-0000-000000000000",
            "created_at": "2026-09-11T00:00:00+00:00",
            "tool": "cloudarc",
            "tool_version": "1.4.0",
            "dedup": False,
            "entry_count": 1,
            "raw_bytes": 3,
            "entries": [entry],
            "methods": {"store": 1},
        }

    def test_valid_metadata_passes(self):
        validate_manifest(self._manifest(mode=0o640, mtime_ns=FIXED_MTIME_NS))

    def test_entry_without_kind_still_validates(self):
        """Archives written before 1.4.0 have no ``kind`` and must keep working."""

        manifest = self._manifest()
        manifest["entries"][0].pop("kind")
        validate_manifest(manifest)

    def test_invalid_kind_is_rejected(self):
        with self.assertRaises(FormatError):
            validate_manifest(self._manifest(kind="hardlink"))

    def test_invalid_mode_is_rejected(self):
        for bad in (0o10000, -1, "644", True):
            with self.subTest(mode=bad):
                with self.assertRaises(FormatError):
                    validate_manifest(self._manifest(mode=bad))

    def test_negative_mtime_is_rejected(self):
        with self.assertRaises(FormatError):
            validate_manifest(self._manifest(mtime_ns=-5))

    def test_symlink_without_target_is_rejected(self):
        with self.assertRaises(FormatError):
            validate_manifest(self._manifest(kind="symlink"))

    def test_target_on_a_regular_file_is_rejected(self):
        with self.assertRaises(FormatError):
            validate_manifest(self._manifest(target="../escape"))

    def test_target_with_nul_byte_is_rejected(self):
        with self.assertRaises(FormatError):
            validate_manifest(self._manifest(kind="symlink", target="a\x00b"))


class RootAndRegressionTests(unittest.TestCase):
    """Defects found by the 1.4.0 review round (red team + diffing reviewer).

    Every test here reproduces a reported defect; the reproduction is kept so the
    same defect cannot come back unnoticed.
    """

    @POSIX_ONLY
    def test_tree_with_only_symlinks_round_trips(self):
        """[blocker] A top level holding nothing but links used to crash unpack.

        No entry created the destination directory (the input root was not an
        entry), so ``os.symlink`` failed with FileNotFoundError and the whole
        archive was lost.
        """

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "onlylinks"
            source.mkdir()
            os.symlink("/etc/hostname", source / "abs")
            os.symlink("missing-target", source / "broken")

            archive = root / "a.vibo"
            result = pack([source], archive)
            self.assertEqual(result["kinds"], {"file": 0, "symlink": 2, "dir": 1})

            restored = root / "out"
            unpack(archive, restored)

            absolute = restored / "onlylinks" / "abs"
            broken = restored / "onlylinks" / "broken"
            self.assertTrue(absolute.is_symlink())
            self.assertEqual(os.readlink(absolute), "/etc/hostname")
            self.assertTrue(broken.is_symlink())
            self.assertFalse(broken.exists())

    @POSIX_ONLY
    def test_root_directory_mode_and_time_are_restored(self):
        """[major] A private 0700 tree used to come back as 0755, silently."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "private"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "note.txt").write_text("x", encoding="utf-8")
            source.chmod(0o700)
            os.utime(source, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))

            archive = root / "a.vibo"
            pack([source], archive)
            restored = root / "out"
            unpack(archive, restored)

            restored_root = restored / "private"
            self.assertEqual(stat.S_IMODE(restored_root.stat().st_mode), 0o700)
            self.assertEqual(restored_root.stat().st_mtime_ns, FIXED_MTIME_NS)

    @POSIX_ONLY
    def test_unpack_apply_over_a_tree_with_a_dangling_symlink(self):
        """[major] The pre-overwrite backup followed dangling links and died.

        ``backup_existing`` called ``shutil.copytree`` without ``symlinks=True``,
        so restoring over a directory that contained a broken link raised
        ``shutil.Error`` and left a half-written ``.bak`` directory behind.
        """

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            source.mkdir()
            (source / "note.txt").write_text("fresh", encoding="utf-8")
            os.symlink("missing.txt", source / "broken")
            archive = root / "a.vibo"
            pack([source], archive)

            out = root / "out"
            (out / "tree").mkdir(parents=True)
            (out / "tree" / "note.txt").write_text("stale", encoding="utf-8")
            os.symlink("gone.txt", out / "tree" / "old-broken")

            result = unpack(archive, out, apply=True)

            self.assertEqual(result["restored"], 3)
            self.assertEqual(
                (out / "tree" / "note.txt").read_text(encoding="utf-8"), "fresh"
            )
            self.assertTrue((out / "tree" / "broken").is_symlink())

    def test_dry_run_leaves_no_staging_directory(self):
        """[major] A dry run used to leave ``.cloudarc-pack-*`` in the output dir."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "note.txt").write_text("x", encoding="utf-8")
            out_dir = root / "out"
            out_dir.mkdir()

            result = pack([source], out_dir / "a.vibo", dry_run=True)

            self.assertTrue(result["dry_run"])
            self.assertEqual([item.name for item in out_dir.iterdir()], [])

    @POSIX_ONLY
    def test_dedup_with_directories_and_symlinks_round_trips(self):
        """[minor] Dedup + the new entry kinds had no test of its own."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "tree"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir(parents=True)
            (source / "a" / "same.txt").write_text("identical\n" * 50, encoding="utf-8")
            (source / "b" / "same2.txt").write_text("identical\n" * 50, encoding="utf-8")
            os.symlink("a/same.txt", source / "l1")
            os.symlink("a/same.txt", source / "l2")

            archive = root / "a.vibo"
            pack([source], archive, dedup=True)
            restored = root / "out"
            result = unpack(archive, restored)

            self.assertEqual(result["metadata_warnings"], [])
            for name in ("l1", "l2"):
                link = restored / "tree" / name
                self.assertTrue(link.is_symlink())
                self.assertEqual(os.readlink(link), "a/same.txt")
            for name in ("a/same.txt", "b/same2.txt"):
                self.assertEqual(
                    (restored / "tree" / name).read_text(encoding="utf-8"),
                    "identical\n" * 50,
                )

    @POSIX_ONLY
    def test_an_out_of_range_mtime_becomes_a_warning(self):
        """[minor] mtime_ns=10**40 is valid per the manifest but breaks os.utime."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            single = root / "note.txt"
            single.write_text("x", encoding="utf-8")
            archive = root / "a.vibo"
            pack([single], archive)

            forged = json.loads(json.dumps(read_manifest(archive)))
            forged["entries"][0]["mtime_ns"] = 10**40
            out = root / "out"

            with patch("core.packer.read_manifest", return_value=forged), patch(
                "core.packer.validate_manifest_index_alignment"
            ):
                result = unpack(archive, out)

            self.assertEqual(result["restored"], 1)
            self.assertTrue(
                any("mtime" in message for message in result["metadata_warnings"]),
                result["metadata_warnings"],
            )
            self.assertEqual((out / "note.txt").read_text(encoding="utf-8"), "x")


if __name__ == "__main__":
    unittest.main()
