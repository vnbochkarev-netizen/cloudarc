from __future__ import annotations

import unittest

from cloud.protocol import (
    build_request,
    execute_sidecar_request,
    sidecar_remote_paths,
)
from core.errors import FormatError


class RemoteProtocolContractTests(unittest.TestCase):
    def _legacy_pair(self):
        return (
            {
                "schema": "cloudarc.manifest",
                "schema_version": 1,
                "archive_id": "a",
                "entries": [],
            },
            {
                "schema": "cloudarc.index",
                "schema_version": 1,
                "archive_id": "a",
                "tokenizer": "unicode-word-v1",
                "documents": {},
                "postings": {},
            },
        )

    def test_semantic_request_exposes_lexical_fallback(self):
        manifest, index = self._legacy_pair()
        response = execute_sidecar_request(
            build_request(
                "search",
                "archive.vibo",
                query="x",
                mode="semantic",
            ),
            manifest,
            index,
        )
        self.assertEqual(response["mode_requested"], "semantic")
        self.assertEqual(response["mode_used"], "lexical")
        self.assertEqual(
            response["fallback_reason"],
            "remote-native-semantic-backend-unavailable",
        )

    def test_explicit_remote_semantic_executor_can_be_used(self):
        manifest, _ = self._legacy_pair()
        manifest["entries"] = [
            {
                "entry_id": "e1",
                "path": "semantic.md",
                "sha256": "0" * 64,
                "chunk_id": "c1",
                "size": 7,
                "chunk_offset": 0,
                "chunk_length": 7,
                "codec": "store",
            }
        ]
        manifest["entry_count"] = 1
        from core.index import build_index

        index = build_index(
            [
                {
                    "entry_id": "e1",
                    "path": "semantic.md",
                    "content": "meaning",
                }
            ],
            archive_id="a",
        )

        def semantic_executor(index, query, limit):
            return [{"entry_id": "e1", "path": "semantic.md", "score": 0.9}]

        response = execute_sidecar_request(
            build_request(
                "search",
                "archive.vibo",
                query="meaning",
                mode="semantic",
            ),
            manifest,
            index,
            semantic_backend=semantic_executor,
        )
        self.assertEqual(response["mode_used"], "semantic")
        self.assertIsNone(response["fallback_reason"])
        self.assertEqual(response["results"][0]["path"], "semantic.md")

    def test_semantic_executor_cannot_mix_entry_id_and_path(self):
        manifest, _ = self._legacy_pair()
        manifest["entries"] = [
            {
                "entry_id": "e1",
                "path": "one.md",
                "sha256": "0" * 64,
                "chunk_id": "c1",
                "size": 3,
                "chunk_offset": 0,
                "chunk_length": 3,
                "codec": "store",
            },
            {
                "entry_id": "e2",
                "path": "two.md",
                "sha256": "1" * 64,
                "chunk_id": "c2",
                "size": 3,
                "chunk_offset": 3,
                "chunk_length": 3,
                "codec": "store",
            },
        ]
        manifest["entry_count"] = 2
        from core.index import build_index

        index = build_index(
            [
                {"entry_id": "e1", "path": "one.md", "content": "one"},
                {"entry_id": "e2", "path": "two.md", "content": "two"},
            ],
            archive_id="a",
        )

        def semantic_executor(index, query, limit):
            return [{"entry_id": "e1", "path": "two.md", "score": 0.9}]

        with self.assertRaises(FormatError):
            execute_sidecar_request(
                build_request(
                    "search",
                    "archive.vibo",
                    query="two",
                    mode="semantic",
                ),
                manifest,
                index,
                semantic_backend=semantic_executor,
            )

    def test_remote_path_and_limit_are_bounded(self):
        with self.assertRaises(FormatError):
            sidecar_remote_paths("../secret.vibo")
        with self.assertRaises(FormatError):
            sidecar_remote_paths("/root/archive.vibo")
        with self.assertRaises(FormatError):
            build_request("search", "archive.vibo", query="x", limit=0)

    def test_invalid_sidecar_schema_is_rejected(self):
        manifest, _ = self._legacy_pair()
        manifest["schema"] = "not-cloudarc"
        with self.assertRaises(FormatError):
            execute_sidecar_request(
                build_request("list", "archive.vibo"),
                manifest,
            )

    def test_manifest_index_semantic_metadata_mismatch_is_rejected(self):
        from core.index import build_index

        manifest, _ = self._legacy_pair()
        manifest["schema_version"] = 2
        manifest["search"] = {
            "index_schema": "cloudarc.index",
            "index_schema_version": 2,
            "default_mode": "lexical",
            "semantic": {
                "schema": "cloudarc.semantic-index",
                "schema_version": 1,
                "backend": "native-vibo",
                "backend_api_version": "cloudarc.native-semantic-v1",
                "model_id": None,
                "dimensions": None,
                "metric": "cosine",
                "status": "unavailable",
                "index_ref": "none",
                "fallback": "lexical-v1",
            },
        }
        index = build_index([], archive_id="a")
        index["semantic"]["reason"] = "different"
        # The stable capability fields still match; changing status must fail.
        index["semantic"]["status"] = "ready"
        with self.assertRaises(FormatError):
            execute_sidecar_request(
                build_request("list", "archive.vibo"),
                manifest,
                index,
            )

    def test_legacy_manifest_must_still_cover_index_documents(self):
        manifest, _ = self._legacy_pair()
        index = {
            "schema": "cloudarc.index",
            "schema_version": 1,
            "archive_id": "a",
            "tokenizer": "unicode-word-v1",
            "documents": {
                "e1": {
                    "entry_id": "e1",
                    "path": "orphan.txt",
                    "excerpt": "orphan",
                    "terms": ["orphan"],
                }
            },
            "postings": {"orphan": ["e1"]},
        }
        with self.assertRaises(FormatError):
            execute_sidecar_request(
                build_request("search", "archive.vibo", query="orphan"),
                manifest,
                index,
            )

    def test_semantic_executor_type_error_is_not_retried(self):
        manifest, _ = self._legacy_pair()
        manifest["entries"] = [
            {
                "entry_id": "e1",
                "path": "semantic.md",
                "sha256": "0" * 64,
                "chunk_id": "c1",
                "size": 7,
                "chunk_offset": 0,
                "chunk_length": 7,
                "codec": "store",
            }
        ]
        manifest["entry_count"] = 1
        from core.index import build_index

        index = build_index(
            [
                {
                    "entry_id": "e1",
                    "path": "semantic.md",
                    "content": "meaning",
                }
            ],
            archive_id="a",
        )
        calls = []

        def semantic_executor(**_kwargs):
            calls.append(True)
            raise TypeError("backend implementation failure")

        response = execute_sidecar_request(
            build_request(
                "search",
                "archive.vibo",
                query="meaning",
                mode="semantic",
            ),
            manifest,
            index,
            semantic_backend=semantic_executor,
        )
        self.assertEqual(calls, [True])
        self.assertEqual(response["mode_used"], "lexical")
        self.assertEqual(
            response["fallback_reason"],
            "remote semantic search failed: TypeError",
        )

    def test_v2_manifest_requires_complete_search_contract(self):
        manifest, _ = self._legacy_pair()
        manifest["schema_version"] = 2
        manifest["search"] = {
            "index_schema": "cloudarc.index",
            "index_schema_version": 2,
            "default_mode": "lexical",
            "lexical": {
                "schema": "cloudarc.lexical-index",
                "schema_version": 1,
                "tokenizer": "unicode-word-v1",
            },
        }
        from core.index import build_index

        index = build_index([], archive_id="a")
        with self.assertRaises(FormatError):
            execute_sidecar_request(
                build_request("list", "archive.vibo"),
                manifest,
                index,
            )
