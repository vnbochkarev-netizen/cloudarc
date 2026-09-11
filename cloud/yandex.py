"""Yandex Disk provider (REST API v1) for CloudArc push/pull/open/fetch.

Credentials
-----------
The OAuth token is read, in order, from

1. the ``token`` argument (tests, embedding),
2. ``$YANDEX_DISK_TOKEN``,
3. the first line of ``$YANDEX_DISK_TOKEN_FILE`` (default
   ``$HOME/.config/cloudarc/yd_token``, mode 600).

Without a token every operation fails closed with ``CloudNotConfigured``. The
token is never printed, logged or included in an error message.

Why ranges matter
-----------------
``read_range`` issues an HTTP ``Range`` request against the file's download URL,
so ``open``/``fetch`` read only the manifest and index of a remote archive.
Searching a remote archive therefore costs a few kilobytes, not a download.
"""

from __future__ import annotations

import fnmatch
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from core.errors import CloudError, CloudNotConfigured, SafetyError
from core.models import FileInfo
from core.safety import require_apply_for_mutation

from .base import DriveCloud


DISK_API = "https://cloud-api.yandex.net/v1/disk"
TOKEN_ENV = "YANDEX_DISK_TOKEN"
ROOT_ENV = "YANDEX_DISK_ROOT"
TOKEN_FILE_ENV = "YANDEX_DISK_TOKEN_FILE"
DEFAULT_TOKEN_FILE = "~/.config/cloudarc/yd_token"
DEFAULT_TIMEOUT = 60
# "disk:/" needs cloud_api:disk.read/write; a token issued with only
# cloud_api:disk.app_folder can still be used with root "app:/" (the folder the
# OAuth application owns on the disk).
DISK_ROOT = "disk:/"
APP_FOLDER_ROOT = "app:/"
LIST_LIMIT = 1000
FIND_MAX_ITEMS = 2000
FIND_MAX_DEPTH = 8
META_FIELDS = "path,size,modified,type,name"
LIST_FIELDS = "limit,total,_embedded.items.path,_embedded.items.size,_embedded.items.modified,_embedded.items.type,_embedded.items.name"


class YandexCloud(DriveCloud):
    name = "yd"

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DISK_API,
        timeout: int = DEFAULT_TIMEOUT,
        root: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.root = self._normalize_root(
            root or os.environ.get(ROOT_ENV) or DISK_ROOT
        )
        self._token = token or self._token_from_environment()
        self._download_hrefs: dict[str, str] = {}

    @staticmethod
    def _normalize_root(root: str) -> str:
        """``disk:/`` for a full-disk token, ``app:/`` for app-folder scope."""

        cleaned = root.strip()
        if not cleaned.endswith("/"):
            cleaned = f"{cleaned}/"
        if not cleaned.endswith(":/"):
            raise CloudNotConfigured(
                f"{ROOT_ENV} must look like 'disk:/' or 'app:/' (got {root!r})"
            )
        return cleaned

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _token_from_environment() -> str | None:
        if os.environ.get(TOKEN_ENV, "").strip():
            return os.environ[TOKEN_ENV].strip()
        path = Path(
            os.environ.get(TOKEN_FILE_ENV) or DEFAULT_TOKEN_FILE
        ).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        token = text.strip().splitlines()[0].strip() if text.strip() else ""
        return token or None

    def _require_token(self) -> str:
        if not self._token:
            raise CloudNotConfigured(
                "Yandex Disk is not configured: set "
                f"{TOKEN_ENV} (or {TOKEN_FILE_ENV} / {DEFAULT_TOKEN_FILE}) "
                "to an OAuth token with disk.write scope"
            )
        return self._token

    # ------------------------------------------------------------- primitives

    def _http(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data=None,
        headers: dict | None = None,
        timeout: int | None = None,
    ) -> tuple[int, dict, bytes]:
        """Single network seam; tests replace this method."""

        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=dict(headers or {}),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:  # pragma: no cover - defensive
                body = b""
            return exc.code, dict(exc.headers or {}), body
        except urllib.error.URLError as exc:
            raise CloudError(f"Yandex Disk request failed: {exc.reason}") from exc

    def _open(self, url: str, *, headers: dict, timeout: int):
        """Streaming GET seam; tests replace this method."""

        request = urllib.request.Request(url, method="GET", headers=headers)
        return urllib.request.urlopen(request, timeout=timeout)

    def _api(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        auth = {"Authorization": f"OAuth {self._require_token()}", "Accept": "application/json"}
        auth.update(headers or {})
        status, _headers, body = self._http(
            method,
            f"{self.base_url}{endpoint}",
            params=params,
            headers=auth,
            timeout=timeout,
        )
        return self._decode(status, body)

    @staticmethod
    def _decode(status: int, body: bytes) -> dict:
        if status in (401, 403):
            raise CloudNotConfigured(
                f"Yandex Disk rejected the token (HTTP {status}): the OAuth token "
                "needs cloud_api:disk.read/write. A token issued with only "
                "cloud_api:disk.app_folder works too - set "
                f"{ROOT_ENV}=app:/ so the adapter stays inside the application folder"
            )
        if status == 404:
            raise CloudError("remote path not found on Yandex Disk")
        if status not in (200, 201, 202, 204):
            detail = body[:200].decode("utf-8", "replace")
            raise CloudError(f"Yandex Disk HTTP {status}: {detail}")
        if not body:
            return {}
        try:
            loaded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CloudError("Yandex Disk returned an unreadable response") from exc
        if not isinstance(loaded, dict):
            raise CloudError("Yandex Disk returned an unexpected response shape")
        return loaded

    # ------------------------------------------------------------------ paths

    @staticmethod
    def _normalize(path: str) -> str:
        """Validate a remote path and return it without a leading slash."""

        if not isinstance(path, str):
            raise SafetyError("remote path must be a string")
        if not path or path == ".":
            return ""
        if "\\" in path or path.startswith("/") or (
            len(path) >= 2 and path[1] == ":"
        ):
            raise SafetyError(f"remote path must be relative: {path}")
        normalized = path.replace("\\", "/").strip("/")
        if any(
            part in {"", ".", ".."} or ":" in part
            for part in normalized.split("/")
        ):
            raise SafetyError(f"invalid remote path: {path}")
        return normalized

    def _api_path(self, relative: str) -> str:
        return f"{self.root}{relative}" if relative else self.root

    @staticmethod
    def _info(raw: dict) -> FileInfo:
        remote = str(raw.get("path", "")).lstrip("/")
        kind = raw.get("type")
        return FileInfo(
            path=remote,
            size=int(raw.get("size") or 0) if kind == "file" else 0,
            modified=raw.get("modified"),
            file_type="file" if kind == "file" else "folder",
            file_id=remote,
        )

    # ---------------------------------------------------------------- provider

    def list_folder(self, path: str) -> list[FileInfo]:
        relative = self._normalize(path)
        payload = self._api(
            "GET",
            "/resources",
            params={
                "path": self._api_path(relative),
                "limit": LIST_LIMIT,
                "fields": LIST_FIELDS,
            },
        )
        embedded = payload.get("_embedded")
        if payload.get("type") == "file" or not isinstance(embedded, dict):
            # A file path has no children; an empty folder has none either.
            if payload.get("type") == "file":
                raise CloudError(f"remote path is a file, not a folder: {path}")
            return []
        items = embedded.get("items") or []
        return [self._info(item) for item in items if isinstance(item, dict)]

    def get_meta(self, path: str) -> FileInfo:
        relative = self._normalize(path)
        if not relative:
            raise CloudError("remote path is required")
        payload = self._api(
            "GET",
            "/resources",
            params={"path": self._api_path(relative), "fields": META_FIELDS},
        )
        return self._info(payload)

    def ensure_folder(self, path: str, *, apply: bool) -> str:
        relative = self._normalize(path)
        if not relative:
            return ""
        try:
            meta = self.get_meta(relative)
        except CloudError:
            meta = None
        if meta is not None:
            if meta.file_type != "folder":
                raise CloudError(f"remote path is a file, not a folder: {path}")
            return relative
        require_apply_for_mutation(apply, "create cloud folder")
        self._api("PUT", "/resources", params={"path": self._api_path(relative)})
        return relative

    def upload(self, local_path: Path, remote_path: str, *, apply: bool) -> str:
        require_apply_for_mutation(apply, "upload")
        source_input = Path(local_path).expanduser()
        if source_input.is_symlink():
            raise SafetyError(f"local upload symlink is not allowed: {source_input}")
        source = source_input.resolve()
        if not source.is_file():
            raise CloudError(f"local upload source is not a file: {source}")
        relative = self._normalize(remote_path)
        if not relative:
            raise CloudError("remote path is required")
        size = source.stat().st_size
        ticket = self._api(
            "GET",
            "/resources/upload",
            params={"path": self._api_path(relative), "overwrite": "true"},
        )
        href = ticket.get("href")
        if not isinstance(href, str) or not href:
            raise CloudError("Yandex Disk did not return an upload URL")

        def body():
            with source.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

        status, _headers, response = self._http(
            "PUT",
            href,
            data=body(),
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            },
            timeout=max(self.timeout, 300),
        )
        if status not in (200, 201, 202):
            detail = response[:200].decode("utf-8", "replace")
            raise CloudError(f"Yandex Disk upload failed: HTTP {status} {detail}")
        self._download_hrefs.pop(relative, None)
        return relative

    def _download_url(self, relative: str) -> str:
        cached = self._download_hrefs.get(relative)
        if cached:
            return cached
        payload = self._api(
            "GET",
            "/resources/download",
            params={"path": self._api_path(relative)},
        )
        href = payload.get("href")
        if not isinstance(href, str) or not href:
            raise CloudError("Yandex Disk did not return a download URL")
        self._download_hrefs[relative] = href
        return href

    def _fetch(self, relative: str, *, range_header: str | None = None) -> tuple[int, bytes]:
        href = self._download_url(relative)
        headers = {"Authorization": f"OAuth {self._require_token()}"}
        if range_header:
            headers["Range"] = range_header
        status, _headers, body = self._http("GET", href, headers=headers)
        if status not in (200, 206):
            detail = body[:200].decode("utf-8", "replace")
            raise CloudError(f"Yandex Disk download failed: HTTP {status} {detail}")
        return status, body

    def download(self, remote_path: str, local_path: Path, *, apply: bool) -> Path:
        require_apply_for_mutation(apply, "download")
        relative = self._normalize(remote_path)
        meta = self.get_meta(relative)
        if meta.file_type != "file":
            raise CloudError(f"remote path is not a file: {remote_path}")
        target_input = Path(local_path).expanduser()
        if target_input.is_symlink():
            raise SafetyError(f"local download symlink is not allowed: {target_input}")
        target = target_input.resolve()
        if target.exists() and target.is_dir():
            raise CloudError(f"local download target is a folder: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        href = self._download_url(relative)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.download-",
            dir=str(target.parent),
        )
        written = 0
        try:
            with os.fdopen(fd, "wb") as file_obj, self._open(
                href,
                headers={"Authorization": f"OAuth {self._require_token()}"},
                timeout=max(self.timeout, 300),
            ) as response:
                if response.status not in (200, 206):
                    raise CloudError(
                        f"Yandex Disk download failed: HTTP {response.status}"
                    )
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    file_obj.write(chunk)
                    written += len(chunk)
            if meta.size and written != meta.size:
                raise CloudError(
                    f"downloaded size mismatch for {remote_path}: "
                    f"expected {meta.size}, got {written}"
                )
            os.replace(temp_name, target)
        except urllib.error.HTTPError as exc:
            raise CloudError(f"Yandex Disk download failed: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise CloudError(f"Yandex Disk download failed: {exc.reason}") from exc
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return target

    def delete(self, path: str, *, apply: bool) -> None:
        require_apply_for_mutation(apply, "delete cloud path")
        relative = self._normalize(path)
        if not relative:
            raise CloudError("remote path is required")
        self._api(
            "DELETE",
            "/resources",
            params={"path": self._api_path(relative), "permanently": "true"},
        )
        self._download_hrefs.pop(relative, None)

    def find(self, pattern: str, folder: str | None = None) -> list[FileInfo]:
        base = self._normalize(folder or "")
        found: list[FileInfo] = []
        queue: list[tuple[str, int]] = [(base, 0)]
        seen = 0
        while queue and seen < FIND_MAX_ITEMS:
            current, depth = queue.pop(0)
            try:
                items = self.list_folder(current)
            except CloudError:
                continue
            for item in items:
                seen += 1
                if item.file_type == "folder":
                    if depth < FIND_MAX_DEPTH:
                        queue.append((item.path, depth + 1))
                    continue
                if fnmatch.fnmatch(Path(item.path).name, pattern):
                    found.append(item)
        return found

    def read_bytes(self, remote_path: str) -> bytes:
        relative = self._normalize(remote_path)
        meta = self.get_meta(relative)
        if meta.file_type != "file":
            raise CloudError(f"remote path is not a file: {remote_path}")
        _status, body = self._fetch(relative)
        if meta.size and len(body) != meta.size:
            raise CloudError(
                f"downloaded size mismatch for {remote_path}: "
                f"expected {meta.size}, got {len(body)}"
            )
        return body

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
        if length == 0:
            return b""
        relative = self._normalize(remote_path)
        status, body = self._fetch(
            relative, range_header=f"bytes={offset}-{offset + length - 1}"
        )
        if status == 206:
            payload = body
        else:
            # A provider (or proxy) that ignores Range must not silently corrupt
            # the read: take the slice locally and verify it is complete.
            payload = body[offset : offset + length]
        if len(payload) != length:
            raise CloudError(
                f"remote byte range is truncated: {remote_path} "
                f"offset={offset} length={length}"
            )
        return payload
