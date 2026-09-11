"""Small shared data models used by CloudArc."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class CompressionHint:
    requested_method: str
    default_codec: str
    searchable: bool
    reason: str


@dataclass(frozen=True)
class SourceFile:
    """One packable entry.

    ``kind`` is ``file``, ``symlink`` or ``dir``. A symlink keeps its target in
    ``link_target``; the payload written into the archive is the target string,
    so a restore recreates the link instead of following it. ``mode`` and
    ``mtime_ns`` carry the permission bits and modification time of the original
    entry (``None`` when metadata capture is disabled).
    """

    source: Path
    archive_path: str
    kind: str = "file"
    link_target: str | None = None
    mode: int | None = None
    mtime_ns: int | None = None


@dataclass(frozen=True)
class FileInfo:
    path: str
    size: int
    modified: Optional[str] = None
    file_type: str = "file"
    file_id: Optional[str] = None
