"""Yandex Disk provider tests (1.4.0).

The provider talks to the Yandex Disk REST API v1. These tests replace the two
network seams (``_http`` for JSON/API calls, ``_open`` for the streaming
download) so the whole adapter is exercised offline: URL shapes, request
bodies, credential handling, error mapping and — importantly — the HTTP Range
reads that let ``open``/``fetch`` search a remote archive without downloading it.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud.yandex import YandexCloud
from core.errors import CloudError, CloudNotConfigured, SafetyError

TOKEN = "fixture-token-value-not-a-secret"


class FakeHttp:
    """Records calls and answers from a routing table."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[dict] = []

    def __call__(self, method, url, *, params=None, data=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params or {},
                "data": data,
                "headers": headers or {},
            }
        )
        key = (method, url.split("?")[0].replace("https://cloud-api.yandex.net/v1/disk", ""))
        responder = self.routes.get(key)
        if responder is None:
            return 404, {}, b"{}"
        if callable(responder):
            return responder(params or {}, data, headers or {})
        status, payload = responder
        return status, {}, json.dumps(payload).encode("utf-8")

    def paths(self) -> list[str]:
        return [call["params"].get("path", "") for call in self.calls]


class FakeStream:
    def __init__(self, payload: bytes, status: int = 200):
        self.payload = payload
        self.status = status
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk, self.offset = self.payload[self.offset :], len(self.payload)
            return chunk
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def make_cloud(routes: dict | None = None, **kwargs) -> tuple[YandexCloud, FakeHttp]:
    cloud = YandexCloud(TOKEN, **kwargs)
    transport = FakeHttp(routes or {})
    cloud._http = transport
    return cloud, transport


class CredentialTests(unittest.TestCase):
    def test_missing_token_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("cloud.yandex.DEFAULT_TOKEN_FILE", "/nonexistent/yd_token"):
                cloud = YandexCloud()
                with self.assertRaises(CloudNotConfigured) as caught:
                    cloud.list_folder("2026")
        self.assertIn("YANDEX_DISK_TOKEN", str(caught.exception))

    def test_token_is_read_from_the_environment(self):
        with patch.dict(os.environ, {"YANDEX_DISK_TOKEN": TOKEN}, clear=True):
            self.assertEqual(YandexCloud()._token, TOKEN)

    def test_token_is_read_from_a_file(self):
        with tempfile.TemporaryDirectory() as temp:
            token_file = Path(temp) / "yd_token"
            token_file.write_text(f"{TOKEN}\n", encoding="utf-8")
            with patch.dict(
                os.environ, {"YANDEX_DISK_TOKEN_FILE": str(token_file)}, clear=True
            ):
                self.assertEqual(YandexCloud()._token, TOKEN)

    def test_token_never_appears_in_error_messages(self):
        cloud, _ = make_cloud(
            {("GET", "/resources"): (401, {"message": "Unauthorized"})}
        )
        with self.assertRaises(CloudNotConfigured) as caught:
            cloud.list_folder("2026")
        self.assertNotIn(TOKEN, str(caught.exception))


class PathSafetyTests(unittest.TestCase):
    def _cloud(self):
        cloud, transport = make_cloud()
        return cloud, transport

    def test_absolute_and_parent_paths_are_rejected(self):
        cloud, _ = self._cloud()
        for bad in ("/etc/passwd", "../escape", "a/../../b", "a//b", "C:stream.vibo", "a\\b"):
            with self.subTest(path=bad):
                with self.assertRaises(SafetyError):
                    cloud.get_meta(bad)


class ResourceTests(unittest.TestCase):
    def test_list_folder_parses_items(self):
        cloud, transport = make_cloud(
            {
                ("GET", "/resources"): (
                    200,
                    {
                        "type": "dir",
                        "_embedded": {
                            "items": [
                                {
                                    "path": "/2026/ROOT/неделя-36/Proj/tree.vibo",
                                    "size": 1024,
                                    "modified": "2026-09-11T18:46:45+00:00",
                                    "type": "file",
                                    "name": "tree.vibo",
                                },
                                {
                                    "path": "/2026/ROOT/неделя-36/Proj/sub",
                                    "type": "dir",
                                    "name": "sub",
                                },
                            ]
                        },
                    },
                )
            }
        )
        items = cloud.list_folder("2026/ROOT/неделя-36/Proj")
        self.assertEqual(items[0].path, "2026/ROOT/неделя-36/Proj/tree.vibo")
        self.assertEqual(items[0].size, 1024)
        self.assertEqual(items[0].file_type, "file")
        self.assertEqual(items[1].file_type, "folder")
        self.assertEqual(items[1].size, 0)
        self.assertEqual(transport.paths()[0], "disk:/2026/ROOT/неделя-36/Proj")

    def test_missing_path_maps_to_cloud_error(self):
        cloud, _ = make_cloud()
        with self.assertRaises(CloudError):
            cloud.get_meta("nope/tree.vibo")

    def test_ensure_folder_requires_apply(self):
        cloud, _ = make_cloud()
        with self.assertRaises(SafetyError):
            cloud.ensure_folder("2026/ROOT", apply=False)

    def test_ensure_folder_creates_when_missing(self):
        cloud, transport = make_cloud(
            {("PUT", "/resources"): (201, {"href": "x", "method": "PUT"})}
        )
        self.assertEqual(cloud.ensure_folder("2026/ROOT", apply=True), "2026/ROOT")
        self.assertEqual(transport.calls[-1]["method"], "PUT")

    def test_ensure_folder_is_idempotent(self):
        cloud, transport = make_cloud(
            {
                ("GET", "/resources"): (
                    200,
                    {"path": "/2026/ROOT", "type": "dir", "name": "ROOT"},
                )
            }
        )
        self.assertEqual(cloud.ensure_folder("2026/ROOT", apply=True), "2026/ROOT")
        self.assertEqual([c["method"] for c in transport.calls], ["GET"])

    def test_delete_requires_apply(self):
        cloud, _ = make_cloud()
        with self.assertRaises(SafetyError):
            cloud.delete("2026/ROOT/x.vibo", apply=False)

    def test_delete_sends_permanent_flag(self):
        cloud, transport = make_cloud({("DELETE", "/resources"): (204, {})})
        cloud.delete("2026/ROOT/x.vibo", apply=True)
        self.assertEqual(transport.calls[-1]["params"]["permanently"], "true")


class UploadTests(unittest.TestCase):
    def _routes(self):
        return {
            ("GET", "/resources/upload"): (
                200,
                {"href": "https://upload.example/put", "method": "PUT"},
            ),
            ("PUT", "https://upload.example/put"): (201, {}),
        }

    def test_upload_is_two_step_and_streams_the_file(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "tree.vibo"
            payload = b"payload-bytes" * 100
            source.write_bytes(payload)
            cloud, transport = make_cloud(self._routes())

            remote = cloud.upload(source, "2026/ROOT/tree.vibo", apply=True)

            self.assertEqual(remote, "2026/ROOT/tree.vibo")
            ticket, push = transport.calls[0], transport.calls[1]
            self.assertEqual(ticket["method"], "GET")
            self.assertEqual(ticket["params"]["path"], "disk:/2026/ROOT/tree.vibo")
            self.assertEqual(ticket["params"]["overwrite"], "true")
            self.assertEqual(push["method"], "PUT")
            self.assertEqual(push["headers"]["Content-Length"], str(len(payload)))
            self.assertEqual(b"".join(push["data"]), payload)

    def test_upload_rejects_a_symlink_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real = root / "real.vibo"
            real.write_bytes(b"x")
            link = root / "link.vibo"
            os.symlink(real, link)
            cloud, _ = make_cloud(self._routes())
            with self.assertRaises(SafetyError):
                cloud.upload(link, "2026/ROOT/link.vibo", apply=True)

    def test_upload_requires_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "tree.vibo"
            source.write_bytes(b"x")
            cloud, _ = make_cloud(self._routes())
            with self.assertRaises(SafetyError):
                cloud.upload(source, "2026/ROOT/tree.vibo", apply=False)


class DownloadTests(unittest.TestCase):
    def _cloud(self, payload: bytes, size: int | None = None):
        routes = {
            ("GET", "/resources"): (
                200,
                {
                    "path": "/2026/ROOT/tree.vibo",
                    "type": "file",
                    "size": len(payload) if size is None else size,
                    "modified": "2026-09-11T00:00:00+00:00",
                    "name": "tree.vibo",
                },
            ),
            ("GET", "/resources/download"): (
                200,
                {"href": "https://down.example/tree.vibo"},
            ),
        }
        cloud, transport = make_cloud(routes)
        cloud._open = lambda url, *, headers, timeout: FakeStream(payload)
        return cloud, transport

    def test_download_writes_the_file(self):
        payload = b"archive-bytes" * 10
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "out" / "tree.vibo"
            cloud, _ = self._cloud(payload)
            result = cloud.download("2026/ROOT/tree.vibo", target, apply=True)
            self.assertEqual(result, target.resolve())
            self.assertEqual(target.read_bytes(), payload)

    def test_download_detects_a_size_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "tree.vibo"
            cloud, _ = self._cloud(b"short", size=999)
            with self.assertRaises(CloudError):
                cloud.download("2026/ROOT/tree.vibo", target, apply=True)
            self.assertFalse(target.exists())

    def test_download_requires_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            cloud, _ = self._cloud(b"x")
            with self.assertRaises(SafetyError):
                cloud.download("2026/ROOT/tree.vibo", Path(temp) / "t.vibo", apply=False)


class RangeReadTests(unittest.TestCase):
    """`open`/`fetch` rely on Range reads: no full download for a search."""

    def _cloud(self, payload: bytes, status: int = 206):
        seen: dict = {}

        def object_body(_params, _data, headers):
            seen["headers"] = headers
            range_header = headers.get("Range")
            if status == 206 and range_header:
                start, end = range_header.split("=")[1].split("-")
                start, end = int(start), int(end)
                return status, {}, payload[start : end + 1]
            return status, {}, payload

        routes = {
            ("GET", "/resources"): (
                200,
                {
                    "path": "/2026/ROOT/tree.vibo",
                    "type": "file",
                    "size": len(payload),
                    "modified": "2026-09-11T00:00:00+00:00",
                    "name": "tree.vibo",
                },
            ),
            ("GET", "/resources/download"): (
                200,
                {"href": "https://down.example/tree.vibo"},
            ),
            ("GET", "https://down.example/tree.vibo"): object_body,
        }
        cloud, transport = make_cloud(routes)
        return cloud, transport, seen

    def test_range_read_uses_an_http_range_header(self):
        payload = bytes(range(256)) * 4
        cloud, _transport, seen = self._cloud(payload)
        chunk = cloud.read_range("2026/ROOT/tree.vibo", 100, 50)
        self.assertEqual(chunk, payload[100:150])
        self.assertEqual(seen["headers"]["Range"], "bytes=100-149")

    def test_range_read_falls_back_when_a_proxy_ignores_range(self):
        payload = bytes(range(256)) * 4
        cloud, _transport, _seen = self._cloud(payload, status=200)
        self.assertEqual(cloud.read_range("2026/ROOT/tree.vibo", 10, 20), payload[10:30])

    def test_truncated_range_is_an_error(self):
        payload = b"tiny"
        cloud, _transport, _seen = self._cloud(payload, status=200)
        with self.assertRaises(CloudError):
            cloud.read_range("2026/ROOT/tree.vibo", 0, 100)

    def test_range_arguments_are_validated(self):
        cloud, _transport, _seen = self._cloud(b"x" * 16)
        for offset, length in ((-1, 1), (0, -1), ("0", 1)):
            with self.subTest(offset=offset, length=length):
                with self.assertRaises(SafetyError):
                    cloud.read_range("2026/ROOT/tree.vibo", offset, length)

    def test_read_bytes_returns_the_whole_object(self):
        payload = b"whole-object" * 5
        cloud, _transport, _seen = self._cloud(payload, status=200)
        self.assertEqual(cloud.read_bytes("2026/ROOT/tree.vibo"), payload)


class FindTests(unittest.TestCase):
    def test_find_recurses_and_matches_names(self):
        def responder(params, _data, _headers):
            path = params.get("path", "")
            if path == "disk:/2026":
                items = [
                    {"path": "/2026/a.vibo", "size": 10, "type": "file", "name": "a.vibo"},
                    {"path": "/2026/sub", "type": "dir", "name": "sub"},
                ]
            elif path == "disk:/2026/sub":
                items = [
                    {"path": "/2026/sub/b.vibo", "size": 20, "type": "file", "name": "b.vibo"},
                    {"path": "/2026/sub/notes.txt", "size": 5, "type": "file", "name": "notes.txt"},
                ]
            else:
                items = []
            return 200, {}, json.dumps(
                {"type": "dir", "_embedded": {"items": items}}
            ).encode("utf-8")

        cloud, _transport = make_cloud({("GET", "/resources"): responder})
        found = sorted(item.path for item in cloud.find("*.vibo", "2026"))
        self.assertEqual(found, ["2026/a.vibo", "2026/sub/b.vibo"])


if __name__ == "__main__":
    unittest.main()
