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
    source: Path
    archive_path: str


@dataclass(frozen=True)
class FileInfo:
    path: str
    size: int
    modified: Optional[str] = None
    file_type: str = "file"
    file_id: Optional[str] = None
