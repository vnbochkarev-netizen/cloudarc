"""Versioned remote search protocol helpers.

The protocol is sidecar-first.  A semantic request is only reported as
semantic when a compatible executor is explicitly supplied; otherwise the
response states that lexical fallback was used.
"""

from __future__ import annotations

import inspect
from pathlib import PurePosixPath
from typing import Any

from core.errors import FormatError
from core.index import (
    normalize_limit,
    lexical_view,
    search_index,
    semantic_descriptor,
    validate_index,
)
from core.manifest import (
    validate_manifest,
    validate_manifest_index_alignment,
)


PROTOCOL = "cloudarc.remote-search"
PROTOCOL_VERSION = "1.1"
SUPPORTED_PROTOCOL_VERSIONS = {"1.0", PROTOCOL_VERSION}


def validate_remote_archive_path(remote_archive: str) -> str:
    if not isinstance(remote_archive, str):
        raise FormatError("remote archive path must be a string")
    normalized = remote_archive.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or "\\" in remote_archive
        or path.is_absolute()
        or ".." in path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(":" in part for part in path.parts)
        or not normalized.lower().endswith(".vibo")
    ):
        raise FormatError(f"invalid remote archive path: {remote_archive!r}")
    if path.as_posix() != normalized:
        raise FormatError(f"remote archive path is not normalized: {remote_archive!r}")
    return normalized


def sidecar_remote_paths(remote_archive: str) -> tuple[str, str]:
    base = validate_remote_archive_path(remote_archive)
    return (
        f"{base}.manifest.json",
        f"{base}.index.json",
    )


def validate_request(request: dict) -> None:
    if not isinstance(request, dict):
        raise FormatError("remote request must be an object")
    if request.get("protocol") != PROTOCOL:
        raise FormatError("unsupported remote search protocol")
    if request.get("protocol_version") not in SUPPORTED_PROTOCOL_VERSIONS:
        raise FormatError("unsupported remote search protocol version")
    operation = request.get("operation")
    if operation not in {"list", "info", "search"}:
        raise FormatError("unsupported remote operation")
    validate_remote_archive_path(request.get("archive", ""))
    limit = request.get("limit", 20)
    normalize_limit(limit)
    if operation == "search":
        if not isinstance(request.get("query", ""), str):
            raise FormatError("remote search query must be a string")
        mode = request.get("mode", "lexical")
        if mode not in {"lexical", "semantic"}:
            raise FormatError(f"unsupported remote search mode: {mode}")


def build_request(
    operation: str,
    remote_archive: str,
    *,
    query: str | None = None,
    limit: int = 20,
    mode: str = "lexical",
) -> dict:
    request = {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "operation": operation,
        "archive": validate_remote_archive_path(remote_archive),
        "limit": limit,
    }
    if query is not None:
        request["query"] = query
    if operation == "search":
        request["mode"] = mode
    validate_request(request)
    return request


def _semantic_search(
    semantic_backend: Any,
    *,
    archive_path: str | None,
    index: dict,
    query: str,
    limit: int,
) -> list[dict] | None:
    """Call an explicitly negotiated remote semantic executor if provided."""

    if semantic_backend is None:
        return None
    function = getattr(semantic_backend, "search_sidecar", None)
    if not callable(function):
        function = semantic_backend if callable(semantic_backend) else None
    if function is None:
        return None
    keyword_call = {
        "index": index,
        "query": query,
        "limit": limit,
        "archive_path": archive_path,
    }
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        signature = None

    if signature is None:
        # Compiled/provider callables may not expose a signature. Use the
        # documented positional fallback without retrying runtime TypeErrors.
        result = function(index, query, limit)
    else:
        try:
            signature.bind(**keyword_call)
        except TypeError:
            try:
                signature.bind(index, query, limit)
            except TypeError as exc:
                raise FormatError(
                    "semantic executor has an incompatible signature"
                ) from exc
            result = function(index, query, limit)
        else:
            result = function(**keyword_call)
    if not isinstance(result, list):
        raise FormatError("semantic executor must return a list")
    return result[:limit]


def execute_sidecar_request(
    request: dict,
    manifest: dict,
    index: dict | None = None,
    *,
    semantic_backend: Any = None,
    archive_path: str | None = None,
) -> dict:
    validate_request(request)
    validate_manifest(manifest)
    if index is not None:
        validate_index(index, expected_archive_id=manifest.get("archive_id"))
        validate_manifest_index_alignment(manifest, index)
    operation = request["operation"]
    envelope = {
        "protocol": PROTOCOL,
        "protocol_version": request["protocol_version"],
        "operation": operation,
        "archive": request["archive"],
    }
    if operation == "list":
        return {**envelope, "entries": manifest.get("entries", [])}
    if operation == "info":
        return {
            **envelope,
            "archive_id": manifest.get("archive_id"),
            "entry_count": manifest.get("entry_count", len(manifest.get("entries", []))),
            "raw_bytes": manifest.get("raw_bytes"),
            "methods": manifest.get("methods", {}),
            "search": manifest.get("search", {}),
        }
    if index is None:
        raise FormatError("search operation requires the index sidecar")

    requested_mode = request.get("mode", "lexical")
    query = request.get("query", "")
    limit = normalize_limit(request.get("limit", 20))
    results = search_index(index, query, limit)
    mode_used = "lexical"
    fallback_reason = None
    if requested_mode == "semantic":
        try:
            semantic_results = _semantic_search(
                semantic_backend,
                archive_path=archive_path,
                index=index,
                query=query,
                limit=limit,
            )
        except Exception as exc:
            semantic_results = None
            fallback_reason = (
                f"remote semantic search failed: {type(exc).__name__}"
            )
        if semantic_results is not None:
            lexical = lexical_view(index)
            documents = lexical.get("documents", {})
            path_by_id = {
                entry_id: document.get("path")
                for entry_id, document in documents.items()
            }
            id_by_path = {
                path: entry_id
                for entry_id, path in path_by_id.items()
            }
            for result in semantic_results:
                if (
                    not isinstance(result, dict)
                    or result.get("path") not in id_by_path
                ):
                    raise FormatError(
                        "semantic executor returned an unknown archive path"
                    )
                entry_id = result.get("entry_id")
                if entry_id is not None:
                    if entry_id not in path_by_id:
                        raise FormatError(
                            "semantic executor returned an unknown entry_id"
                        )
                    if path_by_id[entry_id] != result["path"]:
                        raise FormatError(
                            "semantic executor returned a mismatched entry_id/path"
                        )
            results = semantic_results
            mode_used = "semantic"
        elif fallback_reason is None:
            fallback_reason = "remote-native-semantic-backend-unavailable"

    return {
        **envelope,
        "query": query,
        # ``mode`` is retained as a concise compatibility alias for clients
        # that predate the explicit requested/used fields.
        "mode": mode_used,
        "mode_requested": requested_mode,
        "mode_used": mode_used,
        "fallback_reason": fallback_reason,
        "semantic": semantic_descriptor(index),
        "results": results,
    }
