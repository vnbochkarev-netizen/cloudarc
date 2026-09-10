"""Yandex Disk adapter placeholder with a safe explicit failure."""

from __future__ import annotations

from pathlib import Path

from core.errors import CloudNotConfigured
from core.models import FileInfo

from .base import DriveCloud


class YandexCloud(DriveCloud):
    name = "yd"

    def __init__(self, *_args, **_kwargs):
        pass

    def _unavailable(self):
        raise CloudNotConfigured(
            "Yandex Disk adapter is not configured in the MVP; "
            "provide a token loader and explicit --apply integration"
        )

    def list_folder(self, path: str) -> list[FileInfo]:
        self._unavailable()

    def upload(self, local_path: Path, remote_path: str, *, apply: bool) -> str:
        self._unavailable()

    def download(self, remote_path: str, local_path: Path, *, apply: bool) -> Path:
        self._unavailable()

    def get_meta(self, path: str) -> FileInfo:
        self._unavailable()

    def ensure_folder(self, path: str, *, apply: bool) -> str:
        self._unavailable()

    def delete(self, path: str, *, apply: bool) -> None:
        self._unavailable()

    def find(self, pattern: str, folder: str | None = None) -> list[FileInfo]:
        self._unavailable()

    def read_bytes(self, remote_path: str) -> bytes:
        self._unavailable()

    def read_range(self, remote_path: str, offset: int, length: int) -> bytes:
        self._unavailable()
