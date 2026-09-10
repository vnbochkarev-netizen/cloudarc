from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cloud.local import LocalCloud
from cloud.telemetry import RemoteSearchTelemetry
from cloudarc import _open_remote, _pull, _read_remote_metadata, main
from core.errors import CloudError, FormatError
from core.format import HEADER_SIZE, MAGIC, read_header, sidecar_paths
from core.packer import pack
from core.safety import SafetyError, weekly_folder


class RangeRecordingLocalCloud(LocalCloud):
    def __init__(self, root: Path, *, reject_whole_reads: bool = False):
        super().__init__(root)
        self.ranges: list[tuple[str, int, int]] = []
        self.reject_whole_reads = reject_whole_reads

    def read_range(self, remote_path: str, offset: int, length: int) -> bytes:
        self.ranges.append((remote_path, offset, length))
        return super().read_range(remote_path, offset, length)

    def read_bytes(self, remote_path: str) -> bytes:
        if self.reject_whole_reads:
            raise AssertionError("remote whole-object read was used")
        return super().read_bytes(remote_path)


class CliSafetyTests(unittest.TestCase):
    def _config(self, root: Path) -> tuple[dict, Path]:
        config_path = root / "config.json"
        return (
            {
                "default_disk": "local",
                "cloud_root": str(root / "remote"),
                "stats_root": str(root / "stats"),
                "year": "2026",
                "smartbot_root": "SMARTBOT",
                "week": "01",
                "project": "CloudArc",
            },
            config_path,
        )

    def test_weekly_folder_rejects_traversal_in_root_name(self):
        with self.assertRaises(SafetyError):
            weekly_folder(
                year="2026",
                smartbot_root="../outside",
                week="01",
                project="CloudArc",
            )

    def test_pack_and_unpack_are_preview_only_without_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "docs"
            source.mkdir()
            (source / "note.md").write_text(
                "hello cloudarc",
                encoding="utf-8",
            )
            archive = root / "note.vibo"
            config = root / "config.json"

            with contextlib.redirect_stdout(io.StringIO()):
                preview_code = main(
                    [
                        "--config",
                        str(config),
                        "pack",
                        str(source),
                        "-o",
                        str(archive),
                        "--json",
                    ]
                )
            self.assertEqual(preview_code, 0)
            self.assertFalse(archive.exists())

            with contextlib.redirect_stdout(io.StringIO()):
                apply_code = main(
                    [
                        "--config",
                        str(config),
                        "pack",
                        str(source),
                        "-o",
                        str(archive),
                        "--apply",
                        "--json",
                    ]
                )
            self.assertEqual(apply_code, 0)
            self.assertTrue(archive.exists())

            restored = root / "restored"
            with contextlib.redirect_stdout(io.StringIO()):
                unpack_preview_code = main(
                    [
                        "--config",
                        str(config),
                        "unpack",
                        str(archive),
                        "-o",
                        str(restored),
                        "--json",
                    ]
                )
            self.assertEqual(unpack_preview_code, 0)
            self.assertFalse(restored.exists())

            with contextlib.redirect_stdout(io.StringIO()):
                unpack_apply_code = main(
                    [
                        "--config",
                        str(config),
                        "unpack",
                        str(archive),
                        "-o",
                        str(restored),
                        "--apply",
                        "--json",
                    ]
                )
            self.assertEqual(unpack_apply_code, 0)
            self.assertEqual(
                (restored / "docs" / "note.md").read_text(encoding="utf-8"),
                "hello cloudarc",
            )

    def test_open_rejects_orphan_sidecars_without_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "note.md"
            source.write_text("hello cloudarc", encoding="utf-8")
            archive = root / "archive.vibo"
            pack([source], archive, apply=True)
            manifest_path, index_path = sidecar_paths(archive)
            config, config_path = self._config(root)
            cloud = LocalCloud(root / "remote")
            remote = (
                "2026/SMARTBOT/"
                "\u043d\u0435\u0434\u0435\u043b\u044f-01/CloudArc/archive.vibo"
            )
            cloud.ensure_folder(
                "2026/SMARTBOT/"
                "\u043d\u0435\u0434\u0435\u043b\u044f-01/CloudArc",
                apply=True,
            )
            cloud.upload(
                manifest_path,
                remote + ".manifest.json",
                apply=True,
            )
            cloud.upload(
                index_path,
                remote + ".index.json",
                apply=True,
            )
            args = SimpleNamespace(
                disk="local",
                remote=remote,
                operation="search",
                query="hello",
                limit=20,
                mode="lexical",
            )
            with self.assertRaises(CloudError):
                _open_remote(args, config, config_path)

    def test_pull_rejects_archive_body_mismatching_sidecars(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_source = root / "first.md"
            second_source = root / "second.md"
            first_source.write_text("first body", encoding="utf-8")
            second_source.write_text("second body", encoding="utf-8")
            first_archive = root / "first.vibo"
            second_archive = root / "second.vibo"
            pack([first_source], first_archive, apply=True)
            pack([second_source], second_archive, apply=True)
            first_manifest, first_index = sidecar_paths(first_archive)

            config, config_path = self._config(root)
            cloud = LocalCloud(root / "remote")
            remote = (
                "2026/SMARTBOT/"
                "\u043d\u0435\u0434\u0435\u043b\u044f-01/CloudArc/archive.vibo"
            )
            cloud.ensure_folder(
                "2026/SMARTBOT/"
                "\u043d\u0435\u0434\u0435\u043b\u044f-01/CloudArc",
                apply=True,
            )
            cloud.upload(first_manifest, remote + ".manifest.json", apply=True)
            cloud.upload(first_index, remote + ".index.json", apply=True)
            cloud.upload(second_archive, remote, apply=True)

            args = SimpleNamespace(
                disk="local",
                remote=remote,
                week=None,
                project=None,
                output=str(root / "restored"),
                dry_run=False,
                apply=True,
            )
            with self.assertRaises(FormatError):
                _pull(args, config, config_path)
            self.assertFalse((root / "restored").exists())

    def test_remote_metadata_operations_use_only_metadata_ranges(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "note.md"
            source.write_text("range-only remote search", encoding="utf-8")
            archive = root / "archive.vibo"
            pack([source], archive, apply=True)

            config, config_path = self._config(root)
            cloud = RangeRecordingLocalCloud(
                root / "remote",
                reject_whole_reads=True,
            )
            remote = "2026/SMARTBOT/week-01/CloudArc/archive.vibo"
            cloud.ensure_folder(
                "2026/SMARTBOT/week-01/CloudArc",
                apply=True,
            )
            cloud.upload(archive, remote, apply=True)

            header = read_header(archive)
            expected = [
                (remote, 0, len(MAGIC) + HEADER_SIZE),
                (
                    remote,
                    header["manifest_offset"],
                    header["manifest_length"],
                ),
                (remote, header["index_offset"], header["index_length"]),
            ]
            for operation, query in (
                ("list", None),
                ("info", None),
                ("search", "range-only"),
            ):
                cloud.ranges.clear()
                args = SimpleNamespace(
                    disk="local",
                    remote=remote,
                    operation=operation,
                    query=query,
                    limit=20,
                    mode="lexical",
                )
                with patch("cloudarc._cloud", return_value=cloud):
                    response = _open_remote(args, config, config_path)
                if operation == "search":
                    self.assertEqual(response["results"][0]["path"], "note.md")
                self.assertEqual(cloud.ranges, expected)
                self.assertEqual(response["telemetry"]["range_requests"], 3)
                self.assertEqual(
                    response["telemetry"]["range_bytes"],
                    sum(length for _path, _offset, length in expected),
                )
                self.assertEqual(response["telemetry"]["sidecar_reads"], 0)
                self.assertFalse(response["telemetry"]["data_section_read"])
                for _path, offset, length in cloud.ranges:
                    self.assertLessEqual(offset + length, header["data_offset"])

    def test_remote_metadata_falls_back_to_sidecars_when_ranges_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "note.md"
            source.write_text("sidecar fallback", encoding="utf-8")
            archive = root / "archive.vibo"
            pack([source], archive, apply=True)
            manifest_path, index_path = sidecar_paths(archive)

            config, _config_path = self._config(root)
            cloud = RangeRecordingLocalCloud(root / "remote")
            remote = "2026/SMARTBOT/week-01/CloudArc/archive.vibo"
            cloud.ensure_folder(
                "2026/SMARTBOT/week-01/CloudArc",
                apply=True,
            )
            cloud.upload(archive, remote, apply=True)
            cloud.upload(manifest_path, remote + ".manifest.json", apply=True)
            cloud.upload(index_path, remote + ".index.json", apply=True)

            class NoRangeCloud(RangeRecordingLocalCloud):
                def read_range(
                    self,
                    remote_path: str,
                    offset: int,
                    length: int,
                ) -> bytes:
                    self.ranges.append((remote_path, offset, length))
                    raise NotImplementedError

            fallback_cloud = NoRangeCloud(root / "remote")
            manifest, index = _read_remote_metadata(
                fallback_cloud,
                remote,
            )
            self.assertEqual(manifest["archive_id"], index["archive_id"])
            self.assertEqual(len(fallback_cloud.ranges), 1)

            telemetry = RemoteSearchTelemetry()
            _read_remote_metadata(
                fallback_cloud,
                remote,
                telemetry=telemetry,
            )
            measured = telemetry.as_dict()
            self.assertEqual(measured["range_requests"], 0)
            self.assertEqual(measured["sidecar_reads"], 2)
            self.assertGreater(measured["sidecar_bytes"], 0)

    def test_remote_metadata_does_not_hide_real_range_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "note.md"
            source.write_text("range failure", encoding="utf-8")
            archive = root / "archive.vibo"
            pack([source], archive, apply=True)
            manifest_path, index_path = sidecar_paths(archive)

            cloud_root = root / "remote"
            cloud = RangeRecordingLocalCloud(cloud_root)
            remote = "2026/SMARTBOT/week-01/CloudArc/archive.vibo"
            cloud.ensure_folder(
                "2026/SMARTBOT/week-01/CloudArc",
                apply=True,
            )
            cloud.upload(archive, remote, apply=True)
            cloud.upload(manifest_path, remote + ".manifest.json", apply=True)
            cloud.upload(index_path, remote + ".index.json", apply=True)

            class BrokenRangeCloud(RangeRecordingLocalCloud):
                def read_range(
                    self,
                    remote_path: str,
                    offset: int,
                    length: int,
                ) -> bytes:
                    raise CloudError("provider range request failed")

                def read_bytes(self, remote_path: str) -> bytes:
                    raise AssertionError("sidecar fallback hid a provider error")

            broken = BrokenRangeCloud(cloud_root)
            with self.assertRaises(CloudError):
                _read_remote_metadata(broken, remote)

    def test_open_validates_request_before_provider_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, config_path = self._config(root)
            args = SimpleNamespace(
                disk="local",
                remote="2026/SMARTBOT/week-01/CloudArc/archive.vibo",
                operation="search",
                query="x",
                limit=0,
                mode="lexical",
            )
            with patch(
                "cloudarc._cloud",
                side_effect=AssertionError("provider was read too early"),
            ):
                with self.assertRaises(FormatError):
                    _open_remote(args, config, config_path)
