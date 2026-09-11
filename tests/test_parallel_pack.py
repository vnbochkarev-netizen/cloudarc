"""Process-pool packing: automatic choice, equivalence and the memory warning.

The pool is a measured decision, not a default: on small inputs it loses to the
sequential path (0.76x on 50 tiny files), on real work it wins 2.3-3.7x while
raising peak tree RSS to ~155 MiB (inside the 256 MiB SLO).
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from core.format import read_manifest
from core.packer import POOL_MIN_TOTAL_BYTES, _resolve_workers, pack, unpack


def _make_tree(root: Path, files: int, kib: int, dumps: int = 0) -> None:
    root.mkdir(parents=True, exist_ok=True)
    unit = ("lorem ipsum dolor sit amet consectetur " * 40)[:1024]
    for index in range(files):
        target = kib * 1024
        with (root / f"f{index:04d}.txt").open("w", encoding="utf-8") as handle:
            written = 0
            while written < target:
                handle.write(unit)
                written += len(unit)
    for index in range(dumps):
        (root / f"blob{index}.bin").write_bytes(bytes(range(256)) * 64)


class WorkerResolveTests(unittest.TestCase):
    def test_explicit_value_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp) / "t"
            _make_tree(tree, 1, 1)
            from core.packer import _discover_files

            files, _ = _discover_files([tree])
            self.assertEqual(_resolve_workers(3, files), 3)

    def test_negative_workers_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_workers(-1)

    def test_small_input_stays_sequential(self):
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp) / "t"
            _make_tree(tree, 20, 4)
            result = pack([tree], Path(temp) / "a.vibo")
            self.assertEqual(result["workers"], 1)
            self.assertFalse(
                any("peak RSS budget" in w for w in result["warnings"])
            )

    def test_large_input_uses_the_pool(self):
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp) / "t"
            # two files, just above the measured threshold (a single file cannot
            # be parallelised, so the pool needs at least two)
            _make_tree(tree, 2, (POOL_MIN_TOTAL_BYTES // 1024 // 2) + 1024)
            result = pack([tree], Path(temp) / "a.vibo")
            if (__import__("os").cpu_count() or 1) > 1:
                self.assertGreater(result["workers"], 1)
                self.assertTrue(
                    any("peak RSS budget" in w for w in result["warnings"])
                )
            else:
                self.assertEqual(result["workers"], 1)


class PoolEquivalenceTests(unittest.TestCase):
    def test_workers_1_and_2_produce_equivalent_archives(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tree = root / "tree"
            _make_tree(tree, 60, 32, dumps=2)
            archives = {}
            for workers in (1, 2):
                archive = root / f"w{workers}.vibo"
                pack([tree], archive, dedup=True, workers=workers)
                out = root / f"out{workers}"
                unpack(archive, out)
                manifest = read_manifest(archive)
                archives[workers] = (
                    [(e["path"], e["sha256"], e["chunk_id"], e["codec"], e["stored_size"], e.get("dedup_of"))
                     for e in manifest["entries"]],
                    sorted(
                        (str(p.relative_to(out)), hashlib.sha256(p.read_bytes()).hexdigest())
                        for p in out.rglob("*") if p.is_file()
                    ),
                )
            self.assertEqual(archives[1][0], archives[2][0])  # entries identical
            self.assertEqual(archives[1][1], archives[2][1])  # restored bytes identical


if __name__ == "__main__":
    unittest.main()
