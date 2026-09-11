"""A pack that grows the archive must say so (found on a real document folder).

A 17.6 MB folder of mixed documents produced a 18.9 MB archive with the search
index (+7.6%) and a 13.9 MB archive without it (-20.8%): the index is stored
inside the archive, so on already-compressed data it costs more than the codec
saves. Silent growth is the kind of surprise a backup tool cannot afford.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.packer import pack


class NegativeSavingWarningTests(unittest.TestCase):
    def test_incompressible_input_reports_larger_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "random"
            source.mkdir()
            # already-compressed-looking payload: deterministic pseudo-random bytes
            (source / "blob.bin").write_bytes(bytes(
                (index * 7919 + 13) % 256 for index in range(2 * 1024 * 1024)
            ))
            result = pack([source], root / "a.vibo")
            self.assertLess(result["saved_pct"], 0)
            self.assertTrue(
                any("LARGER than the input" in w for w in result["warnings"]),
                result["warnings"],
            )
            self.assertTrue(any("--no-index" in w for w in result["warnings"]))

    def test_index_is_what_makes_the_archive_bigger(self):
        """Same payload twice: with the index it is larger, without it smaller."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "docs"
            source.mkdir()
            for index in range(40):
                (source / f"note{index:02d}.md").write_text(
                    f"# note {index}\n" + ("bounded memory packing line " * 200),
                    encoding="utf-8",
                )
            with_index = pack([source], root / "with.vibo")
            without = pack([source], root / "without.vibo", index=False)

            self.assertLess(without["packed_bytes"], with_index["packed_bytes"])
            self.assertLess(without["header"]["index_length"], with_index["header"]["index_length"])
            self.assertFalse(
                any("LARGER than the input" in w for w in without["warnings"]),
                without["warnings"],
            )


if __name__ == "__main__":
    unittest.main()
