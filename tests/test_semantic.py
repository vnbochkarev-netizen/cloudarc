from __future__ import annotations

import types
import unittest

from core.semantic import (
    NativeSemanticBackend,
    _semantic_capability,
)
from core.index import NATIVE_SEMANTIC_API_VERSION


class NativeSemanticContractTests(unittest.TestCase):
    def test_generic_search_is_not_semantic_capability(self):
        module = types.ModuleType("generic_vibo")
        module.search = lambda query, archive, limit: []
        capability, reason = _semantic_capability(module, {})
        self.assertFalse(capability["supported"])
        self.assertIn("no explicit", reason)

    def test_explicit_native_capability_is_callable(self):
        module = types.ModuleType("compatible_vibo")
        module.CLOUDARC_CAPABILITIES = {
            "index_schema_versions": [2],
            "semantic_search": {
                "supported": True,
                "api_version": NATIVE_SEMANTIC_API_VERSION,
                "search_function": "cloudarc_semantic_search",
            },
        }

        def cloudarc_semantic_search(query: str, archive: str, limit: int):
            return [
                {
                    "entry_id": "e1",
                    "path": "docs/meaning.md",
                    "score": 0.91,
                }
            ]

        module.cloudarc_semantic_search = cloudarc_semantic_search
        capability, reason = _semantic_capability(
            module,
            module.CLOUDARC_CAPABILITIES,
        )
        self.assertFalse(reason)
        backend = NativeSemanticBackend(module, capability)
        results = backend.search("archive.vibo", "meaning", 5)
        self.assertEqual(results[0]["path"], "docs/meaning.md")
        self.assertEqual(results[0]["excerpt"], "")

