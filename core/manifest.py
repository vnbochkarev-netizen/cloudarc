"""Manifest schema, path validation and canonical serialization."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

from .errors import FormatError
from .index import (
    NATIVE_SEMANTIC_API_VERSION,
    SEMANTIC_SCHEMA,
    SEMANTIC_SCHEMA_VERSION,
)


MANIFEST_SCHEMA = "cloudarc.manifest"
MANIFEST_SCHEMA_VERSION = 2
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = {1, 2}
ALLOWED_CODECS = {"store", "deflate", "zstd"}
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


ALLOWED_ENTRY_KINDS = {"file", "symlink", "dir"}
MAX_LINK_TARGET_BYTES = 4096


def canonical_json_bytes(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def pretty_json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_archive_path(value: str) -> str:
    if not isinstance(value, str):
        raise FormatError("archive path must be a string")
    path = PurePosixPath(value)
    normalized = path.as_posix()
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or (len(value) >= 2 and value[1] == ":")
        or value.strip() == ""
        or normalized != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise FormatError(f"invalid archive path: {value!r}")
    return normalized


def _validate_search_contract(manifest: dict) -> None:
    search = manifest.get("search")
    if search is None:
        # Legacy v1 manifests predate the explicit search contract.
        if manifest.get("schema_version") == 1:
            return
        raise FormatError("manifest search contract is required")
    if not isinstance(search, dict):
        raise FormatError("manifest search contract must be an object")
    if search.get("index_schema") != "cloudarc.index":
        raise FormatError("manifest references an unsupported index schema")
    if search.get("index_schema_version") not in {1, 2}:
        raise FormatError("manifest references an unsupported index version")
    if search.get("default_mode", "lexical") not in {"lexical", "semantic"}:
        raise FormatError("manifest default search mode is invalid")
    lexical = search.get("lexical")
    index_schema_version = search.get("index_schema_version")
    if manifest.get("schema_version") == 2 and index_schema_version == 2:
        if lexical is None:
            raise FormatError("manifest lexical search metadata is required")
        if search.get("semantic") is None:
            raise FormatError("manifest semantic metadata is required")
    if lexical is not None:
        if not isinstance(lexical, dict):
            raise FormatError("manifest lexical search metadata must be an object")
        if lexical.get("schema") != "cloudarc.lexical-index":
            raise FormatError("unsupported manifest lexical schema")
        if lexical.get("schema_version") != 1:
            raise FormatError("unsupported manifest lexical schema version")
    semantic = search.get("semantic")
    if semantic is not None:
        if not isinstance(semantic, dict):
            raise FormatError("manifest semantic metadata must be an object")
        if semantic.get("schema") != SEMANTIC_SCHEMA:
            raise FormatError("unsupported manifest semantic schema")
        if semantic.get("schema_version") != SEMANTIC_SCHEMA_VERSION:
            raise FormatError("unsupported manifest semantic schema version")
        if semantic.get("backend") != "native-vibo":
            raise FormatError("unsupported manifest semantic backend")
        if semantic.get("backend_api_version") != NATIVE_SEMANTIC_API_VERSION:
            raise FormatError("unsupported manifest semantic backend API version")
        if semantic.get("status") not in {"unavailable", "ready"}:
            raise FormatError("invalid manifest semantic status")
        if semantic.get("metric") not in {"cosine", "dot", "l2"}:
            raise FormatError("invalid manifest semantic metric")
        if semantic.get("index_ref") not in {
            "none",
            "native",
            "embedded",
            "sidecar",
            "external",
        }:
            raise FormatError("invalid manifest semantic index_ref")
        if semantic.get("fallback") != "lexical-v1":
            raise FormatError("manifest semantic fallback must be lexical-v1")
        if semantic.get("status") == "ready":
            if (
                not isinstance(semantic.get("model_id"), str)
                or not semantic["model_id"].strip()
            ):
                raise FormatError(
                    "ready manifest semantic metadata requires model_id"
                )
            dimensions = semantic.get("dimensions")
            if (
                isinstance(dimensions, bool)
                or not isinstance(dimensions, int)
                or dimensions <= 0
            ):
                raise FormatError(
                    "ready manifest semantic metadata requires positive dimensions"
                )
            if semantic.get("index_ref") == "none":
                raise FormatError(
                    "ready manifest semantic metadata requires an index_ref"
                )
        else:
            if semantic.get("index_ref") != "none":
                raise FormatError(
                    "unavailable manifest semantic metadata must use index_ref=none"
                )
            if semantic.get("model_id") is not None:
                raise FormatError(
                    "unavailable manifest semantic metadata cannot declare model_id"
                )
            if semantic.get("dimensions") is not None:
                raise FormatError(
                    "unavailable manifest semantic metadata cannot declare dimensions"
                )


def validate_manifest(
    manifest: dict,
    *,
    expected_archive_id: str | None = None,
    data_length: int | None = None,
) -> None:
    """Validate a manifest before it is trusted locally or remotely."""

    if not isinstance(manifest, dict):
        raise FormatError("manifest must be an object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise FormatError("unsupported manifest schema")
    version = manifest.get("schema_version")
    if version not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        raise FormatError("unsupported manifest schema version")

    archive_id = manifest.get("archive_id")
    if not isinstance(archive_id, str) or not archive_id.strip():
        raise FormatError("manifest archive_id must be a non-empty string")
    if expected_archive_id is not None and archive_id != expected_archive_id:
        raise FormatError("manifest archive_id does not match archive")

    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise FormatError("manifest entries must be a list")
    seen_paths: set[str] = set()
    seen_ids: set[str] = set()
    symlink_paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise FormatError("manifest entry must be an object")
        path = validate_archive_path(entry.get("path", ""))
        if path in seen_paths:
            raise FormatError(f"duplicate archive path: {path}")
        seen_paths.add(path)

        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise FormatError(f"invalid entry_id: {path}")
        if entry_id in seen_ids:
            raise FormatError(f"duplicate entry_id: {entry_id}")
        seen_ids.add(entry_id)

        kind = entry.get("kind", "file")
        if not isinstance(kind, str) or kind not in ALLOWED_ENTRY_KINDS:
            raise FormatError(f"invalid entry kind: {path}")
        mode = entry.get("mode")
        if mode is not None and (
            isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode < 0
            or mode > 0o7777
        ):
            raise FormatError(f"invalid mode for manifest entry: {path}")
        if "mtime_ns" in entry and not _is_nonnegative_int(entry["mtime_ns"]):
            raise FormatError(f"invalid mtime_ns for manifest entry: {path}")
        if kind == "symlink":
            target = entry.get("target")
            if not isinstance(target, str) or not target or "\x00" in target:
                raise FormatError(f"symlink entry needs a target: {path}")
            if len(target.encode("utf-8", "surrogateescape")) > MAX_LINK_TARGET_BYTES:
                raise FormatError(f"symlink target is too long: {path}")
        elif "target" in entry:
            raise FormatError(f"only symlink entries may declare a target: {path}")

        for field in (
            "sha256",
            "chunk_id",
            "size",
            "chunk_offset",
            "chunk_length",
        ):
            if field not in entry:
                raise FormatError(f"manifest entry missing {field}: {path}")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not HEX_SHA256_RE.fullmatch(digest):
            raise FormatError(f"invalid sha256 for manifest entry: {path}")
        if not isinstance(entry.get("chunk_id"), str) or not entry["chunk_id"]:
            raise FormatError(f"invalid chunk_id for manifest entry: {path}")
        for field in ("size", "chunk_offset", "chunk_length"):
            if not _is_nonnegative_int(entry.get(field)):
                raise FormatError(f"invalid {field} for manifest entry: {path}")
        if "stored_size" in entry and entry["stored_size"] != entry["chunk_length"]:
            raise FormatError(f"stored_size mismatch for manifest entry: {path}")
        if "codec" in entry and entry["codec"] not in ALLOWED_CODECS:
            raise FormatError(f"unsupported codec for manifest entry: {path}")
        if "searchable" in entry and not isinstance(entry["searchable"], bool):
            raise FormatError(f"invalid searchable flag for manifest entry: {path}")
        if "changing" in entry and not isinstance(entry["changing"], bool):
            raise FormatError(f"invalid changing flag for manifest entry: {path}")
        if kind == "symlink":
            symlink_paths.append(path)

        if data_length is not None:
            end = entry["chunk_offset"] + entry["chunk_length"]
            if end > data_length:
                raise FormatError(
                    f"manifest chunk is outside data section: {path}"
                )

    # "Create a link, then write through it" is the classic archive escape:
    # an archive that declares a symlink and then stores entries *below* that
    # link is refused before a single byte is written.
    for symlink_path in symlink_paths:
        prefix = f"{symlink_path}/"
        for other in seen_paths:
            if other.startswith(prefix):
                raise FormatError(
                    f"archive writes through a symlink: {symlink_path}"
                )

    if "entry_count" in manifest:
        if not _is_nonnegative_int(manifest["entry_count"]):
            raise FormatError("manifest entry_count must be a non-negative integer")
        if manifest["entry_count"] != len(entries):
            raise FormatError("manifest entry_count does not match entries")
    if "raw_bytes" in manifest and not _is_nonnegative_int(manifest["raw_bytes"]):
        raise FormatError("manifest raw_bytes must be a non-negative integer")
    methods = manifest.get("methods")
    if methods is not None:
        if not isinstance(methods, dict) or any(
            not isinstance(key, str) or not _is_nonnegative_int(value)
            for key, value in methods.items()
        ):
            raise FormatError("manifest methods must map codec names to counts")

    _validate_search_contract(manifest)


def validate_manifest_index_alignment(manifest: dict, index: dict) -> None:
    """Ensure manifest search metadata describes the embedded index."""

    if not isinstance(index, dict):
        raise FormatError("index must be an object")
    search = manifest.get("search")
    if isinstance(search, dict):
        if search.get("index_schema_version") not in {
            None,
            index.get("schema_version"),
        }:
            raise FormatError("manifest and index schema versions do not match")

        declared = search.get("semantic")
        actual = index.get("semantic")
        if isinstance(declared, dict) and isinstance(actual, dict):
            for field in (
                "schema",
                "schema_version",
                "backend",
                "backend_api_version",
                "model_id",
                "dimensions",
                "metric",
                "status",
                "index_ref",
                "fallback",
            ):
                if declared.get(field) != actual.get(field):
                    raise FormatError(
                        f"manifest/index semantic metadata mismatch: {field}"
                    )

    lexical = index.get("lexical", index)
    documents = lexical.get("documents", {}) if isinstance(lexical, dict) else {}
    manifest_entries = {
        entry["entry_id"]: entry for entry in manifest.get("entries", [])
    }
    for entry_id, document in documents.items():
        if entry_id not in manifest_entries:
            raise FormatError(
                f"index references an unknown manifest entry_id: {entry_id}"
            )
        if document.get("path") != manifest_entries[entry_id].get("path"):
            raise FormatError(
                f"manifest/index path mismatch for entry_id: {entry_id}"
            )
