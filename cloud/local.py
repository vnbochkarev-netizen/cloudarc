"""Local filesystem provider used for deterministic MVP tests and dry-runs."""

from __future__ import annotations

import fnmatch
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from core.errors import CloudError, SafetyError
from core.models import FileInfo
from core.safety import ensure_within, require_apply_for_mutation

from .base import DriveCloud


class LocalCloud(DriveCloud):
    name = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def _remote(self, path: str) -> Path:
        if not isinstance(path, str):
            raise SafetyError("remote path must be a string")
        if "\\" in path or path.startswith("/") or (
            len(path) >= 2 and path[1] == ":"
        ):
            raise SafetyError(f"remote path must be relative: {path}")
        normalized = path.replace("\\", "/")
        if not normalized or normalized == ".":
            return self.root
        if any(
            part in {"", ".", ".."} or ":" in part
            for part in normalized.split("/")
        ):
            raise SafetyError(f"invalid remote path: {path}")
        candidate = self.root / normalized
        current = self.root
        for part in normalized.split("/"):
            current = current / part
            if current.is_symlink():
                raise SafetyError(f"remote symlink is not allowed: {path}")
        return ensure_within(self.root, candidate)

    def list_folder(self, path: str) -> list[FileInfo]:
        folder = self._remote(path)
        if not folder.exists():
            return []
        if not folder.is_dir():
            raise CloudError(f"remote path is not a folder: {path}")
        rows: list[FileInfo] = []
        for item in sorted(folder.iterdir()):
            if item.is_symlink():
                raise SafetyError(f"remote symlink is not allowed: {item}")
            stat = item.stat()
            rows.append(
                FileInfo(
                    path=item.relative_to(self.root).as_posix(),
                    size=stat.st_size if item.is_file() else 0,
                    modified=datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    file_type="file" if item.is_file() else "folder",
                    file_id=item.relative_to(self.root).as_posix(),
                )
            )
        return rows

    def upload(self, local_path: Path, remote_path: str, *, apply: bool) -> str:
        require_apply_for_mutation(apply, "upload")
        source_input = local_path.expanduser()
        if source_input.is_symlink():
            raise SafetyError(f"local upload symlink is not allowed: {source_input}")
        source = source_input.resolve()
        if not source.is_file():
            raise CloudError(f"local upload source is not a file: {source}")
        target = self._remote(remote_path)
        if target.exists():
            if target.is_dir():
                raise CloudError(f"remote upload target is a folder: {remote_path}")
            from core.safety import backup_existing

            backup_existing(target)
        self.root.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.upload-",
            dir=str(target.parent),
        )
        try:
            os.close(fd)
            shutil.copy2(source, temp_name)
            os.replace(temp_name, target)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return target.relative_to(self.root).as_posix()

    def download(
        self,
        remote_path: str,
        local_path: Path,
        *,
        apply: bool,
    ) -> Path:
        require_apply_for_mutation(apply, "download")
        source = self._remote(remote_path)
        if not source.is_file():
            raise CloudError(f"remote file not found: {remote_path}")
        target_input = local_path.expanduser()
        if target_input.is_symlink():
            raise SafetyError(
                f"local download symlink is not allowed: {target_input}"
            )
        target = target_input.resolve()
        if target.exists():
            if target.is_dir():
                raise CloudError(f"local download target is a folder: {target}")
            from core.safety import backup_existing

            backup_existing(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.download-",
            dir=str(target.parent),
        )
        try:
            os.close(fd)
            shutil.copy2(source, temp_name)
            os.replace(temp_name, target)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return target

    def ensure_folder(self, path: str, *, apply: bool) -> str:
        folder = self._remote(path)
        if not folder.exists():
            require_apply_for_mutation(apply, "create cloud folder")
            folder.mkdir(parents=True, exist_ok=True)
        return folder.relative_to(self.root).as_posix()

    def get_meta(self, path: str) -> FileInfo:
        target = self._remote(path)
        if not target.exists():
            raise CloudError(f"remote path not found: {path}")
        stat = target.stat()
        return FileInfo(
            path=target.relative_to(self.root).as_posix(),
            size=stat.st_size if target.is_file() else 0,
            modified=datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(),
            file_type="file" if target.is_file() else "folder",
            file_id=target.relative_to(self.root).as_posix(),
        )

    def delete(self, path: str, *, apply: bool) -> None:
        require_apply_for_mutation(apply, "delete cloud path")
        target = self._remote(path)
        if not target.exists():
            return
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    def find(self, pattern: str, folder: str | None = None) -> list[FileInfo]:
        base = self._remote(folder or "")
        if not base.exists():
            return []
        rows: list[FileInfo] = []
        for item in base.rglob("*"):
            if item.is_file() and fnmatch.fnmatch(item.name, pattern):
                rows.append(self.get_meta(item.relative_to(self.root).as_posix()))
        return rows

    def read_bytes(self, remote_path: str) -> bytes:
        source = self._remote(remote_path)
        if not source.is_file():
            raise CloudError(f"remote file not found: {remote_path}")
        return source.read_bytes()

    def read_range(self, remote_path: str, offset: int, length: int) -> bytes:
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or isinstance(length, bool)
            or not isinstance(length, int)
            or offset < 0
            or length < 0
        ):
            raise SafetyError("remote byte range must be non-negative integers")
        source = self._remote(remote_path)
        if not source.is_file():
            raise CloudError(f"remote file not found: {remote_path}")
        with source.open("rb") as file_obj:
            file_obj.seek(offset)
            payload = file_obj.read(length)
        if len(payload) != length:
            raise CloudError(
                f"remote byte range is truncated: {remote_path} "
                f"offset={offset} length={length}"
            )
        return payload
