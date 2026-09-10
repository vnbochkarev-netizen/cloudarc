from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cloud.local import LocalCloud
from cloud.protocol import build_request, execute_sidecar_request
from core.errors import FormatError, SafetyError
from core.format import sidecar_paths
from core.packer import pack


class LocalCloudProtocolTests(unittest.TestCase):
    def test_sidecar_search_does_not_need_archive_body(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "notes.md"
            source.write_text("Quarterly cloud report", encoding="utf-8")
            archive = root / "archive.vibo"
            pack([source], archive)
            manifest_path, index_path = sidecar_paths(archive)

            cloud = LocalCloud(root / "remote")
            remote = "2026/SMARTBOT/неделя-01/CloudArc/archive.vibo"
            cloud.ensure_folder("2026/SMARTBOT/неделя-01/CloudArc", apply=True)
            cloud.upload(archive, remote, apply=True)
            cloud.upload(manifest_path, remote + ".manifest.json", apply=True)
            cloud.upload(index_path, remote + ".index.json", apply=True)

            manifest = json.loads(
                cloud.read_bytes(remote + ".manifest.json").decode("utf-8")
            )
            index = json.loads(
                cloud.read_bytes(remote + ".index.json").decode("utf-8")
            )
            response = execute_sidecar_request(
                build_request("search", remote, query="quarterly"),
                manifest,
                index,
            )
            self.assertEqual(response["operation"], "search")
            self.assertEqual(response["results"][0]["path"], "notes.md")

    def test_local_provider_rejects_mutation_without_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local = root / "source.txt"
            local.write_text("x", encoding="utf-8")
            cloud = LocalCloud(root / "remote")
            with self.assertRaises(SafetyError):
                cloud.upload(local, "folder/source.txt", apply=False)

    def test_local_provider_rejects_colon_path_segments(self):
        with tempfile.TemporaryDirectory() as temp:
            cloud = LocalCloud(Path(temp) / "remote")
            with self.assertRaises(SafetyError):
                cloud.get_meta("folder:stream.vibo")

    def test_mixed_sidecars_are_rejected(self):
        manifest = {
            "schema": "cloudarc.manifest",
            "schema_version": 1,
            "archive_id": "a",
            "entries": [],
        }
        index = {
            "schema": "cloudarc.index",
            "schema_version": 1,
            "archive_id": "b",
            "documents": {},
            "postings": {},
        }
        with self.assertRaises(FormatError):
            execute_sidecar_request(
                build_request("search", "archive.vibo", query="x"),
                manifest,
                index,
            )
