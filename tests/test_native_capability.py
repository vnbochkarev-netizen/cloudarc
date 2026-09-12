"""Regression: a native semantic backend must not depend on the Python version.

``core.semantic`` used to refuse every interpreter except CPython 3.11
("native ViBo package requires CPython 3.11"), so a module could not be used even
where a matching build existed, and semantic search never ran anywhere. These
tests pin the new behaviour: the check is real (import + published capability)
and the diagnostics name the build that exists and the one that is required.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from core.native import get_native_semantic_backend, probe_native
from core.packer import search_archive_result
from core.semantic import interpreter_tag

MODULE_SOURCE = '''\
"""Test native module: publishes the cloudarc.native-semantic-v1 contract."""

NATIVE_SEMANTIC_API_VERSION = "cloudarc.native-semantic-v1"
CLOUDARC_CAPABILITIES = {
    "semantic_search": {
        "supported": True,
        "api_version": "cloudarc.native-semantic-v1",
        "index_schema_versions": [1, 2],
        "search_function": "cloudarc_semantic_search",
        "model_ids": ["test-model"],
    }
}


def cloudarc_semantic_search(query, archive=None, limit=10, **kwargs):
    return [{"entry_id": "e00000001", "path": "docs/a.txt", "score": 0.9,
             "excerpt": "test"}]
'''


def _write_module(directory: Path, name: str = "vibo_archive") -> None:
    (directory / f"{name}.py").write_text(MODULE_SOURCE, encoding="utf-8")


class InterpreterIndependentProbeTests(unittest.TestCase):
    def test_no_hard_python_version_gate(self):
        """The probe must not refuse because of the interpreter version."""

        result = probe_native()
        self.assertEqual(result["interpreter"], interpreter_tag())
        self.assertNotIn("requires CPython", result["reason"])
        self.assertNotIn("requires CPython", result["semantic_reason"])

    def test_module_available_on_current_interpreter(self):
        """A clean module on the running interpreter is accepted as native."""

        with tempfile.TemporaryDirectory() as tmp:
            _write_module(Path(tmp))
            result = probe_native(tmp)
        self.assertTrue(result["available"], result)
        self.assertEqual(result["reason"], "native archive module imported")

    def test_capability_is_negotiated_not_guessed(self):
        """Availability comes from the declared capability, not from a version."""

        with tempfile.TemporaryDirectory() as tmp:
            _write_module(Path(tmp))
            result = probe_native(tmp)
        self.assertTrue(result["semantic_available"], result)
        capability = result["capabilities"]["semantic_search"]
        self.assertEqual(capability["api_version"], "cloudarc.native-semantic-v1")
        self.assertEqual(capability["search_function"], "cloudarc_semantic_search")

    def test_missing_build_reports_available_build_tags(self):
        """Without a build for this interpreter, name the builds that exist."""

        with tempfile.TemporaryDirectory() as tmp:
            tag = "cpython-311" if sys.version_info[:2] != (3, 11) else "cpython-312"
            (Path(tmp) / f"vibo_archive.{tag}-x86_64-linux-gnu.so").write_bytes(b"\x00")
            result = probe_native(tmp)
        self.assertFalse(result["available"])
        self.assertIn(interpreter_tag(), result["reason"])
        self.assertIn("builds found in", result["reason"])
        self.assertIn(tag, result["reason"])

    def test_foreign_extension_is_diagnosed_as_build_mismatch(self):
        """A broken/foreign build is classified as a CPython version mismatch."""

        with tempfile.TemporaryDirectory() as tmp:
            # a file with a .so suffix that is not a module for this interpreter
            (Path(tmp) / "vibo_archive.so").write_bytes(b"not an elf")
            result = probe_native(tmp)
        self.assertFalse(result["available"])
        self.assertTrue(
            "built for a different CPython" in result["reason"]
            or "import failed" in result["reason"],
            result["reason"])


class NativeSemanticBackendTests(unittest.TestCase):
    def test_backend_is_constructed_from_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_module(Path(tmp))
            backend = get_native_semantic_backend(tmp)
            self.assertIsNotNone(backend)
            results = backend.search("/tmp/no-such-archive.vibo", "query", 5)
            self.assertEqual(results[0]["path"], "docs/a.txt")

    def test_result_rows_are_normalised(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_module(Path(tmp))
            backend = get_native_semantic_backend(tmp)
            self.assertIsNotNone(backend)
            row = backend.search("x.vibo", "q", 1)[0]
            self.assertEqual(row["entry_id"], "e00000001")
            self.assertIn("score", row)


class PackerDescriptorTests(unittest.TestCase):
    def test_index_descriptor_is_never_a_version_gate(self):
        """The index descriptor must not carry a version-gate reason."""

        from core.packer import _semantic_for_index

        with tempfile.TemporaryDirectory() as tmp:
            module_dir = Path(tmp) / "mod"
            module_dir.mkdir()
            _write_module(module_dir)
            desc = _semantic_for_index(str(module_dir), "vibo_archive")
        # the test module publishes no descriptor probe -> unavailable, but honest
        self.assertIn(desc["status"], {"ready", "unavailable"})
        self.assertNotIn("requires CPython", json.dumps(desc, ensure_ascii=False))

    def test_index_descriptor_without_backend_stays_unavailable(self):
        from core.packer import _semantic_for_index

        with tempfile.TemporaryDirectory() as tmp:
            desc = _semantic_for_index(str(Path(tmp)), "no_such_module")
        self.assertEqual(desc["status"], "unavailable")
        self.assertTrue(desc["reason"])


class SearchFallbackTests(unittest.TestCase):
    def test_semantic_mode_without_backend_falls_back_to_lexical(self):
        """With no native module the mode degrades honestly to lexical."""

        from core.packer import pack

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            (docs / "a.txt").write_text("dividends of the bank shares", encoding="utf-8")
            archive = root / "a.vibo"
            pack([str(docs)], str(archive), apply=True)
            result = search_archive_result(
                str(archive), "dividends", mode="semantic",
                native_module_path=None, native_module_name="no_such_module")
        self.assertEqual(result["mode_requested"], "semantic")
        self.assertEqual(result["mode_used"], "lexical")
        self.assertTrue(result["fallback_reason"])
        self.assertTrue(result["results"])


if __name__ == "__main__":
    unittest.main()
