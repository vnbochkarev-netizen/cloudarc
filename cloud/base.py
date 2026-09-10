"""Provider-neutral cloud contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from core.models import FileInfo


class DriveCloud(ABC):
    """Minimal provider contract used by push/pull/open/fetch."""

    name = "unknown"

    @abstractmethod
    def list_folder(self, path: str) -> list[FileInfo]:
        raise NotImplementedError

    @abstractmethod
    def upload(self, local_path: Path, remote_path: str, *, apply: bool) -> str:
        raise NotImplementedError

    @abstractmethod
    def download(
        self,
        remote_path: str,
        local_path: Path,
        *,
        apply: bool,
    ) -> Path:
        raise NotImplementedError

    @abstractmethod
    def get_meta(self, path: str) -> FileInfo:
        raise NotImplementedError

    @abstractmethod
    def ensure_folder(self, path: str, *, apply: bool) -> str:
        raise NotImplementedError

    @abstractmethod
    def delete(self, path: str, *, apply: bool) -> None:
        raise NotImplementedError

    @abstractmethod
    def find(self, pattern: str, folder: str | None = None) -> list[FileInfo]:
        raise NotImplementedError

    def read_bytes(self, remote_path: str) -> bytes:
        """Read a sidecar or other small remote object without downloading it."""

        raise NotImplementedError

    def read_range(self, remote_path: str, offset: int, length: int) -> bytes:
        """Read a bounded byte range when the provider supports range access."""

        raise NotImplementedError
