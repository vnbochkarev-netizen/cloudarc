#!/usr/bin/env python3
"""Verify a CloudArc restore against the original tree, entry by entry.

Compares, for every manifest entry: kind, content hash, permission bits and
modification time. Written for the 1.4.0 metadata work — a restore that returns
file *contents* but drops modes and links is not a restore.

Usage:
    python3 cloudarc_verify_restore.py <archive.vibo> <original-root> <restored-root> [--prefix NAME]
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

def _cloudarc_tree() -> Path:
    """Find a CloudArc source tree (the script only needs ``core.format``)."""

    candidates = []
    if os.environ.get("CLOUDARC_SRC"):
        candidates.append(Path(os.environ["CLOUDARC_SRC"]))
    candidates.append(Path("/root/cloudarc-src"))
    candidates.extend(sorted(Path.home().glob(".cache/cloudarc/*"), reverse=True))
    for candidate in candidates:
        if (candidate / "core" / "format.py").is_file():
            return candidate
    raise SystemExit(
        "не нашёл дерево CloudArc: задайте CLOUDARC_SRC=/путь/к/дереву"
    )


sys.path.insert(0, str(_cloudarc_tree()))
from core.format import read_manifest  # noqa: E402  (needs the CloudArc tree)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 2
    archive, original_root, restored_root = map(Path, argv[1:4])
    prefix = None
    if "--prefix" in argv:
        prefix = argv[argv.index("--prefix") + 1]
    if prefix is None:
        # The archive was created from the input directory itself: its name is
        # the first path component of every entry.
        prefix = read_manifest(archive)["entries"][0]["path"].split("/")[0]
    manifest = read_manifest(archive)
    problems: list[tuple[str, str]] = []
    checked = 0
    for entry in manifest["entries"]:
        relative = entry["path"].split("/", 1)[1] if "/" in entry["path"] else ""
        original = original_root / relative
        restored = restored_root / prefix / relative
        checked += 1
        kind = entry.get("kind", "file")
        if kind == "symlink":
            if not restored.is_symlink():
                problems.append((entry["path"], "ссылка не восстановлена"))
                continue
            if os.readlink(restored) != entry.get("target"):
                problems.append((entry["path"], "цель ссылки не совпала"))
            continue
        if kind == "dir":
            if not restored.is_dir():
                problems.append((entry["path"], "папка не восстановлена"))
            elif (
                entry.get("mode") is not None
                and stat.S_IMODE(restored.stat().st_mode) != entry["mode"]
            ):
                problems.append((entry["path"], "права папки не совпали"))
            continue
        if not restored.is_file():
            problems.append((entry["path"], "файл не восстановлен"))
            continue
        if restored.stat().st_size != entry["size"]:
            problems.append((entry["path"], "размер не совпал"))
            continue
        if digest(restored) != entry["sha256"]:
            problems.append((entry["path"], "хеш содержимого не совпал"))
            continue
        if os.path.exists(original):
            if os.path.getsize(original) != entry["size"]:
                problems.append((entry["path"], "оригинал изменился с момента упаковки"))
                continue
            if digest(original) != entry["sha256"]:
                problems.append((entry["path"], "архив не совпал с оригиналом"))
                continue
            if (
                stat.S_IMODE(original.stat().st_mode)
                != stat.S_IMODE(restored.stat().st_mode)
            ):
                problems.append((entry["path"], "права файла не совпали"))
                continue
            if original.stat().st_mtime_ns != restored.stat().st_mtime_ns:
                problems.append((entry["path"], "время изменения не совпало"))
                continue
        else:
            problems.append((entry["path"], "оригинала больше нет — сверка прав пропущена"))
    kinds: dict[str, int] = {}
    for entry in manifest["entries"]:
        kinds[entry.get("kind", "file")] = kinds.get(entry.get("kind", "file"), 0) + 1
    print(json.dumps({
        "archive": str(archive),
        "entries_checked": checked,
        "kinds": kinds,
        "problems": problems[:20],
        "problem_count": len(problems),
        "verdict": "OK" if not problems else "ПРОБЛЕМЫ",
    }, ensure_ascii=False, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
