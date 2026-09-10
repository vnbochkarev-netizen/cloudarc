from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.errors import FormatError, SafetyError
from core.format import (
    HEADER_SIZE,
    MAGIC,
    read_header,
    read_index,
    read_manifest,
    sidecar_paths,
    stream_entry_to_file,
)
from core.index import StreamingTextIndexer
from core.packer import (
    info_archive,
    list_archive,
    pack,
    search_archive,
    search_archive_result,
    unpack,
)


class CoreArchiveTests(unittest.TestCase):
    def test_pack_unpack_round_trip_and_search(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "documents"
            source.mkdir()
            (source / "report.md").write_text(
                "# Quarterly report\nCloudArc storage savings.",
                encoding="utf-8",
            )
            (source / "data.bin").write_bytes(bytes(range(64)))
            archive = root / "archive.vibo"

            result = pack([source], archive, dedup=True)
            self.assertTrue(archive.exists())
            self.assertEqual(result["entry_count"], 2)
            self.assertEqual(read_header(archive)["format_version"], 1)
            self.assertEqual(len(read_manifest(archive)["entries"]), 2)
            self.assertEqual(read_index(archive)["schema"], "cloudarc.index")

            manifest_sidecar, index_sidecar = sidecar_paths(archive)
            self.assertTrue(manifest_sidecar.exists())
            self.assertTrue(index_sidecar.exists())

            matches = search_archive(archive, "quarterly report")
            self.assertEqual(matches[0]["path"], "documents/report.md")

            restored = root / "restored"
            unpack(archive, restored)
            self.assertEqual(
                (restored / "documents" / "report.md").read_text(encoding="utf-8"),
                "# Quarterly report\nCloudArc storage savings.",
            )
            self.assertEqual(
                (restored / "documents" / "data.bin").read_bytes(),
                bytes(range(64)),
            )

    def test_streaming_pack_and_unpack_avoid_whole_file_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "large.txt"
            payload = (
                ("bounded-memory noise " * 120000)
                + " unique-streaming-search-term "
                + ("tail " * 120000)
            ).encode("utf-8")
            source.write_bytes(payload)
            archive = root / "large.vibo"
            restored = root / "restored"

            # The production path must use bounded chunk reads.  A whole-file
            # Path.read_bytes call would fail this test immediately.
            with patch(
                "pathlib.Path.read_bytes",
                side_effect=AssertionError("whole-file read is not allowed"),
            ):
                pack([source], archive, apply=True)
                unpack(archive, restored, apply=True)

            self.assertEqual(
                search_archive(archive, "unique-streaming-search-term")[0][
                    "path"
                ],
                "large.txt",
            )
            self.assertEqual((restored / "large.txt").read_bytes(), payload)

    def test_streaming_unpack_does_not_call_compatibility_reader(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "note.txt"
            source.write_text("streamed payload", encoding="utf-8")
            archive = root / "archive.vibo"
            pack([source], archive, apply=True)

            with patch(
                "core.format.read_entry_bytes",
                side_effect=AssertionError("compatibility reader was used"),
            ):
                result = unpack(archive, root / "restored", apply=True)

            self.assertEqual(result["restored"], 1)
            self.assertEqual(
                (root / "restored" / "note.txt").read_text(encoding="utf-8"),
                "streamed payload",
            )

    def test_deflate_expansion_writes_bounded_chunks(self):
        class BoundedSink:
            def __init__(self, limit):
                self.limit = limit
                self.max_write = 0
                self.total = 0

            def write(self, payload):
                if len(payload) > self.limit:
                    raise AssertionError("decoder emitted an oversized block")
                self.max_write = max(self.max_write, len(payload))
                self.total += len(payload)
                return len(payload)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "expansion.txt"
            raw_size = 8 * 1024 * 1024
            with source.open("wb") as file_obj:
                for _ in range(8):
                    file_obj.write(b"x" * (1024 * 1024))
            archive = root / "expansion.vibo"
            pack([source], archive, apply=True)
            entry = read_manifest(archive)["entries"][0]
            self.assertEqual(entry["codec"], "deflate")

            buffer_size = 64 * 1024
            sink = BoundedSink(buffer_size)
            restored = stream_entry_to_file(
                archive,
                entry,
                sink,
                buffer_size=buffer_size,
            )
            self.assertEqual(restored, raw_size)
            self.assertEqual(sink.total, raw_size)
            self.assertLessEqual(sink.max_write, buffer_size)

    def test_unpack_rejects_corrupt_embedded_index_before_writing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "note.txt"
            source.write_text("integrity", encoding="utf-8")
            archive = root / "archive.vibo"
            pack([source], archive, apply=True)
            header = read_header(archive)
            with archive.open("r+b") as file_obj:
                file_obj.seek(header["index_offset"])
                original = file_obj.read(1)
                file_obj.seek(header["index_offset"])
                file_obj.write(b"{" if original != b"{" else b"}")

            with self.assertRaises(FormatError):
                unpack(archive, root / "restored", apply=True)
            self.assertFalse((root / "restored").exists())

    def test_streaming_indexer_preserves_utf8_and_token_boundaries(self):
        indexer = StreamingTextIndexer(max_excerpt_chars=12)
        payload = "Привет cloudarc streamingneedle".encode("utf-8")
        for byte in payload:
            indexer.feed_bytes(bytes([byte]))
        indexer.finish()

        self.assertIn("привет", indexer.terms)
        self.assertIn("cloudarc", indexer.terms)
        self.assertIn("streamingneedle", indexer.terms)
        self.assertEqual(indexer.excerpt, "Привет cloud")

    def test_dedup_reuses_chunk(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "a.txt"
            second = root / "b.txt"
            first.write_text("same content", encoding="utf-8")
            second.write_text("same content", encoding="utf-8")
            archive = root / "archive.vibo"

            pack([first, second], archive, dedup=True)
            entries = list_archive(archive)
            self.assertEqual(entries[0]["chunk_id"], entries[1]["chunk_id"])
            self.assertEqual(entries[1]["dedup_of"], entries[0]["entry_id"])

    def test_overwrite_requires_apply_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "a.txt"
            source.write_text("one", encoding="utf-8")
            archive = root / "archive.vibo"
            pack([source], archive)
            source.write_text("two", encoding="utf-8")

            with self.assertRaises(SafetyError):
                pack([source], archive)
            pack([source], archive, apply=True)
            self.assertTrue(list(root.glob("archive.vibo.bak-*")))

    def test_format_magic_and_header_size(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "a.txt"
            source.write_text("hello", encoding="utf-8")
            archive = root / "archive.vibo"
            pack([source], archive)
            with archive.open("rb") as file_obj:
                self.assertEqual(file_obj.read(len(MAGIC)), MAGIC)
                self.assertEqual(len(file_obj.read(HEADER_SIZE)), HEADER_SIZE)

    def test_info_has_real_byte_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "a.txt"
            source.write_text("hello " * 100, encoding="utf-8")
            archive = root / "archive.vibo"
            pack([source], archive)
            info = info_archive(archive)
            self.assertGreater(info["raw_bytes"], 0)
            self.assertGreater(info["packed_bytes"], 0)

    def test_index_searches_beyond_excerpt_limit_and_publishes_v2(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "late.txt"
            source.write_text(
                "prefix " + ("noise " * 1000) + "needle-at-the-end",
                encoding="utf-8",
            )
            archive = root / "archive.vibo"
            pack([source], archive)

            index = read_index(archive)
            manifest = read_manifest(archive)
            self.assertEqual(index["schema_version"], 2)
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(
                search_archive(archive, "needle-at-the-end")[0]["path"],
                "late.txt",
            )

    def test_semantic_request_reports_lexical_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "note.md"
            source.write_text("semantic fallback", encoding="utf-8")
            archive = root / "archive.vibo"
            pack([source], archive)

            result = search_archive_result(
                archive,
                "fallback",
                mode="semantic",
            )
            self.assertEqual(result["mode_requested"], "semantic")
            self.assertEqual(result["mode_used"], "lexical")
            self.assertTrue(result["fallback_reason"])
            self.assertEqual(result["semantic"]["status"], "unavailable")

    def test_pack_rejects_output_inside_input_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "documents"
            source.mkdir()
            (source / "a.txt").write_text("a", encoding="utf-8")
            with self.assertRaises(SafetyError):
                pack([source], source / "nested" / "archive.vibo")

    def test_pack_rejects_top_level_symlink_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            target.write_text("a", encoding="utf-8")
            link = root / "link.txt"
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(SafetyError):
                pack([link], root / "archive.vibo", apply=True)

    def test_unpack_rejects_output_containing_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "a.txt"
            source.write_text("a", encoding="utf-8")
            archive = root / "archive.vibo"
            pack([source], archive)
            with self.assertRaises(SafetyError):
                unpack(archive, root, apply=True)
