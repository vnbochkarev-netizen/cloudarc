"""CloudArc .vibo container format and integrity helpers.

Physical layout (all offsets are absolute from byte zero):

    0                 MAGIC = b"VIBO\n"
    5                 fixed-size UTF-8 JSON header (8192 bytes)
    8197              canonical manifest JSON
    manifest_end     canonical versioned index JSON
    index_end        concatenated compressed data chunks

The fixed header makes the manifest/index discoverable with one small range
request.  Sidecar files mirror the manifest and index for providers without
reliable range reads.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .errors import FormatError
from .index import validate_index
from .manifest import (
    MANIFEST_SCHEMA,
    canonical_json_bytes,
    pretty_json_bytes,
    sha256_bytes,
    validate_manifest_index_alignment,
    validate_manifest,
)


MAGIC = b"VIBO\n"
HEADER_SIZE = 8192
FORMAT_NAME = "cloudarc"
FORMAT_VERSION = 1
# Archive version 1 stores file contents only. Version 2 declares that the
# manifest carries entry metadata (kind, mode, mtime_ns, symlink targets) which a
# 1.x reader cannot honour - it would restore a directory as an empty file and a
# symlink as a small text file. Such readers check this number and refuse the
# archive, so a 1.4.0 archive fails loudly on an old engine instead of restoring
# something wrong. Archives whose entries are all plain files stay version 1 and
# remain readable by 1.3.x.
METADATA_FORMAT_VERSION = 2
SUPPORTED_FORMAT_VERSIONS = {FORMAT_VERSION, METADATA_FORMAT_VERSION}


def _header_block(header: dict) -> bytes:
    encoded = canonical_json_bytes(header)
    if len(encoded) > HEADER_SIZE:
        raise FormatError(
            f"header is {len(encoded)} bytes; maximum is {HEADER_SIZE}"
        )
    return encoded + b" " * (HEADER_SIZE - len(encoded))


def _validate_header(header: dict, *, file_size: int | None = None) -> None:
    if not isinstance(header, dict):
        raise FormatError("VIBO header must be an object")
    if header.get("container") != FORMAT_NAME:
        raise FormatError("unsupported VIBO container")
    if header.get("format_version") not in SUPPORTED_FORMAT_VERSIONS:
        raise FormatError(
            "unsupported VIBO format version "
            f"{header.get('format_version')!r}: this build reads "
            f"{sorted(SUPPORTED_FORMAT_VERSIONS)}"
        )
    if not isinstance(header.get("archive_id"), str) or not header["archive_id"]:
        raise FormatError("VIBO header archive_id is missing")
    if header.get("header_offset") != len(MAGIC):
        raise FormatError("invalid VIBO header offset")
    if header.get("header_length") != HEADER_SIZE:
        raise FormatError("invalid VIBO header length")

    integer_fields = (
        "manifest_offset",
        "manifest_length",
        "index_offset",
        "index_length",
        "data_offset",
        "data_length",
        "archive_size",
        "entry_count",
    )
    for field in integer_fields:
        value = header.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise FormatError(f"invalid VIBO header field: {field}")

    expected_manifest_offset = len(MAGIC) + HEADER_SIZE
    if header["manifest_offset"] != expected_manifest_offset:
        raise FormatError("invalid VIBO manifest offset")
    if header["index_offset"] != (
        header["manifest_offset"] + header["manifest_length"]
    ):
        raise FormatError("invalid VIBO index offset")
    if header["data_offset"] != header["index_offset"] + header["index_length"]:
        raise FormatError("invalid VIBO data offset")
    if header["archive_size"] != header["data_offset"] + header["data_length"]:
        raise FormatError("invalid VIBO archive size")
    if file_size is not None:
        if isinstance(file_size, bool) or not isinstance(file_size, int):
            raise FormatError("invalid VIBO file size")
        if file_size != header["archive_size"]:
            raise FormatError("VIBO archive size does not match the header")
    if header["manifest_length"] <= 0 or header["index_length"] <= 0:
        raise FormatError("VIBO manifest/index sections must not be empty")


def parse_header_bytes(
    payload: bytes,
    *,
    file_size: int | None = None,
) -> dict:
    """Parse a fixed header fetched from a local file or a byte range."""

    if not isinstance(payload, (bytes, bytearray)):
        raise FormatError("VIBO header payload must be bytes")
    required = len(MAGIC) + HEADER_SIZE
    if len(payload) < required:
        raise FormatError("truncated VIBO header")
    if bytes(payload[: len(MAGIC)]) != MAGIC:
        raise FormatError("missing VIBO magic")
    block = bytes(payload[len(MAGIC) : required])
    try:
        header = json.loads(block.rstrip(b" ").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormatError("invalid VIBO header JSON") from exc
    _validate_header(header, file_size=file_size)
    return header


def _read_header_from_file(file_obj: BinaryIO) -> dict:
    file_obj.seek(0)
    return parse_header_bytes(file_obj.read(len(MAGIC) + HEADER_SIZE))


def read_header(path: Path) -> dict:
    path = Path(path)
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise FormatError(f"cannot stat VIBO archive: {path}") from exc
    with path.open("rb") as file_obj:
        header = _read_header_from_file(file_obj)
    _validate_header(header, file_size=file_size)
    return header


def _read_section(path: Path, offset: int, length: int) -> bytes:
    if offset < 0 or length < 0:
        raise FormatError("invalid VIBO section range")
    with path.open("rb") as file_obj:
        file_obj.seek(offset)
        payload = file_obj.read(length)
    if len(payload) != length:
        raise FormatError("truncated VIBO section")
    return payload


def _parse_manifest_payload(header: dict, raw: bytes) -> dict:
    if not isinstance(raw, (bytes, bytearray)):
        raise FormatError("VIBO manifest payload must be bytes")
    if len(raw) != header["manifest_length"]:
        raise FormatError("truncated VIBO manifest section")
    raw = bytes(raw)
    if sha256_bytes(raw) != header.get("manifest_sha256"):
        raise FormatError("manifest checksum mismatch")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormatError("invalid manifest JSON") from exc
    validate_manifest(
        manifest,
        expected_archive_id=header["archive_id"],
        data_length=header["data_length"],
    )
    if manifest.get("entry_count", len(manifest["entries"])) != header["entry_count"]:
        raise FormatError("manifest/header entry count mismatch")
    return manifest


def _parse_index_payload(header: dict, raw: bytes) -> dict:
    if not isinstance(raw, (bytes, bytearray)):
        raise FormatError("VIBO index payload must be bytes")
    if len(raw) != header["index_length"]:
        raise FormatError("truncated VIBO index section")
    raw = bytes(raw)
    if sha256_bytes(raw) != header.get("index_sha256"):
        raise FormatError("index checksum mismatch")
    try:
        index = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormatError("invalid index JSON") from exc
    validate_index(index, expected_archive_id=header["archive_id"])
    return index


def parse_metadata_sections(
    header: dict,
    manifest_payload: bytes,
    index_payload: bytes,
) -> tuple[dict, dict]:
    """Validate and decode manifest/index bytes obtained by range reads."""

    _validate_header(header)
    manifest = _parse_manifest_payload(header, manifest_payload)
    index = _parse_index_payload(header, index_payload)
    validate_manifest_index_alignment(manifest, index)
    return manifest, index


def read_manifest(path: Path) -> dict:
    path = Path(path)
    header = read_header(path)
    raw = _read_section(path, header["manifest_offset"], header["manifest_length"])
    return _parse_manifest_payload(header, raw)


def read_index(path: Path) -> dict:
    path = Path(path)
    header = read_header(path)
    raw = _read_section(path, header["index_offset"], header["index_length"])
    return _parse_index_payload(header, raw)


def _decompress(payload: bytes, codec: str) -> bytes:
    """Compatibility helper for callers that already hold one payload."""

    if codec == "store":
        return payload
    if codec == "deflate":
        try:
            return zlib.decompress(payload)
        except zlib.error as exc:
            raise FormatError("invalid deflate data") from exc
    if codec == "zstd":
        try:
            import zstandard as zstd  # type: ignore
        except Exception as exc:
            raise FormatError(
                "zstd entry requires the optional zstandard package"
            ) from exc
        try:
            return zstd.ZstdDecompressor().decompress(payload)
        except Exception as exc:
            raise FormatError("invalid zstd data") from exc
    raise FormatError(f"unsupported codec: {codec}")


class _LimitedReader:
    """Bound a file object to one compressed chunk."""

    def __init__(self, file_obj: BinaryIO, length: int):
        self.file_obj = file_obj
        self.remaining = length

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size is None or size < 0:
            size = self.remaining
        size = min(size, self.remaining)
        payload = self.file_obj.read(size)
        self.remaining -= len(payload)
        return payload

    def readinto(self, buffer) -> int:
        if self.remaining <= 0:
            return 0
        view = memoryview(buffer)
        size = min(len(view), self.remaining)
        payload = self.file_obj.read(size)
        view[: len(payload)] = payload
        self.remaining -= len(payload)
        return len(payload)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False


def _entry_range(header: dict, entry: dict) -> tuple[int, int]:
    if not isinstance(entry, dict):
        raise FormatError("manifest entry must be an object")
    offset = entry.get("chunk_offset")
    length = entry.get("chunk_length")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or isinstance(length, bool)
        or not isinstance(length, int)
        or length < 0
        or offset + length > header["data_length"]
    ):
        raise FormatError(f"entry chunk is outside data section: {entry.get('path')}")
    return offset, length


def stream_entry_to_file(
    path: Path,
    entry: dict,
    output: BinaryIO,
    *,
    verify: bool = True,
    buffer_size: int = 1024 * 1024,
) -> int:
    """Stream one entry into a writable binary file and return raw byte count."""

    if isinstance(buffer_size, bool) or not isinstance(buffer_size, int):
        raise FormatError("stream buffer size must be an integer")
    if buffer_size <= 0:
        raise FormatError("stream buffer size must be positive")

    path = Path(path)
    header = read_header(path)
    offset, length = _entry_range(header, entry)
    digest = hashlib.sha256()
    raw_size = 0

    def write_raw(payload: bytes) -> None:
        nonlocal raw_size
        if not payload:
            return
        output.write(payload)
        digest.update(payload)
        raw_size += len(payload)

    with path.open("rb") as file_obj:
        file_obj.seek(header["data_offset"] + offset)
        codec = entry.get("codec", "store")
        if codec == "store":
            remaining = length
            while remaining:
                payload = file_obj.read(min(buffer_size, remaining))
                if not payload:
                    raise FormatError(
                        f"truncated data chunk: {entry.get('path')}"
                    )
                write_raw(payload)
                remaining -= len(payload)
        elif codec == "deflate":
            decompressor = zlib.decompressobj()
            remaining = length
            while remaining:
                payload = file_obj.read(min(buffer_size, remaining))
                if not payload:
                    raise FormatError(
                        f"truncated data chunk: {entry.get('path')}"
                    )
                remaining -= len(payload)
                pending = payload
                while pending:
                    try:
                        expanded = decompressor.decompress(
                            pending,
                            buffer_size,
                        )
                    except zlib.error as exc:
                        raise FormatError("invalid deflate data") from exc
                    write_raw(expanded)
                    tail = decompressor.unconsumed_tail
                    if tail and not expanded and len(tail) == len(pending):
                        raise FormatError("deflate decoder made no progress")
                    pending = tail
            while True:
                try:
                    expanded = decompressor.decompress(b"", buffer_size)
                except zlib.error as exc:
                    raise FormatError("invalid deflate data") from exc
                if not expanded:
                    break
                write_raw(expanded)
            try:
                write_raw(decompressor.flush(buffer_size))
            except zlib.error as exc:
                raise FormatError("invalid deflate data") from exc
            if (
                not decompressor.eof
                or decompressor.unused_data
                or decompressor.unconsumed_tail
            ):
                raise FormatError("invalid deflate data")
        elif codec == "zstd":
            try:
                import zstandard as zstd  # type: ignore
            except Exception as exc:
                raise FormatError(
                    "zstd entry requires the optional zstandard package"
                ) from exc
            limited = _LimitedReader(file_obj, length)
            try:
                reader = zstd.ZstdDecompressor().stream_reader(limited)
                while True:
                    payload = reader.read(buffer_size)
                    if not payload:
                        break
                    write_raw(payload)
                reader.close()
            except Exception as exc:
                raise FormatError("invalid zstd data") from exc
            if limited.remaining:
                raise FormatError("invalid zstd data")
        else:
            raise FormatError(f"unsupported codec: {codec}")

    expected_size = entry.get("size")
    if (
        isinstance(expected_size, bool)
        or (expected_size is not None and not isinstance(expected_size, int))
        or (isinstance(expected_size, int) and expected_size < 0)
    ):
        raise FormatError(f"invalid entry size: {entry.get('path')}")
    if isinstance(expected_size, int) and raw_size != expected_size:
        raise FormatError(f"entry size mismatch: {entry.get('path')}")
    if verify:
        if digest.hexdigest() != entry.get("sha256"):
            raise FormatError(f"entry checksum mismatch: {entry.get('path')}")
    return raw_size


def read_entry_bytes(path: Path, entry: dict, *, verify: bool = True) -> bytes:
    output = io.BytesIO()
    stream_entry_to_file(path, entry, output, verify=verify)
    return output.getvalue()


def sidecar_paths(path: Path) -> tuple[Path, Path]:
    base = Path(str(path))
    return (
        Path(f"{base}.manifest.json"),
        Path(f"{base}.index.json"),
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as file_obj:
            file_obj.write(payload)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write_sidecars(path: Path, manifest: dict, index: dict) -> tuple[Path, Path]:
    """Write both sidecars from validated embedded metadata.

    The function never trusts an existing sidecar and rolls back replaced
    files if the second replacement fails.
    """

    validate_manifest(manifest)
    validate_index(index, expected_archive_id=manifest.get("archive_id"))
    validate_manifest_index_alignment(manifest, index)
    manifest_path, index_path = sidecar_paths(path)
    manifest_bytes = pretty_json_bytes(manifest)
    index_bytes = pretty_json_bytes(index)

    parent = manifest_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".cloudarc-sidecars-", dir=parent))
    staged_manifest = staging / manifest_path.name
    staged_index = staging / index_path.name
    old_values: dict[Path, bytes | None] = {}
    replaced: list[Path] = []
    try:
        staged_manifest.write_bytes(manifest_bytes)
        staged_index.write_bytes(index_bytes)
        for target in (manifest_path, index_path):
            old_values[target] = target.read_bytes() if target.exists() else None
        os.replace(staged_manifest, manifest_path)
        replaced.append(manifest_path)
        os.replace(staged_index, index_path)
        replaced.append(index_path)
    except Exception:
        for target in reversed(replaced):
            old = old_values[target]
            try:
                if old is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write(target, old)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return manifest_path, index_path


def read_sidecar(path: Path, kind: str) -> dict:
    manifest_path, index_path = sidecar_paths(path)
    if kind not in {"manifest", "index"}:
        raise FormatError(f"unsupported sidecar kind: {kind}")
    selected = manifest_path if kind == "manifest" else index_path
    try:
        value = json.loads(selected.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FormatError(f"missing {kind} sidecar: {selected}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormatError(f"invalid {kind} sidecar: {selected}") from exc
    if kind == "manifest":
        validate_manifest(value)
    else:
        validate_index(value)
    return value


def write_archive(
    path: Path,
    manifest: dict,
    index: dict,
    chunks: dict[str, bytes],
    *,
    format_version: int = FORMAT_VERSION,
) -> dict:
    """Write a CloudArc archive and atomically publish it."""

    path = Path(path)
    if manifest.get("archive_id") != index.get("archive_id"):
        raise FormatError("manifest and index archive_id values do not match")

    chunk_offsets: dict[str, int] = {}
    data_length = 0
    for chunk_id in dict.fromkeys(
        entry["chunk_id"] for entry in manifest["entries"]
    ):
        if chunk_id not in chunks:
            raise FormatError(f"missing data chunk: {chunk_id}")
        chunk_offsets[chunk_id] = data_length
        data_length += len(chunks[chunk_id])

    for entry in manifest["entries"]:
        entry["chunk_offset"] = chunk_offsets[entry["chunk_id"]]
        entry["chunk_length"] = len(chunks[entry["chunk_id"]])
        entry["stored_size"] = len(chunks[entry["chunk_id"]])

    validate_manifest(manifest, data_length=data_length)
    validate_index(index, expected_archive_id=manifest.get("archive_id"))
    validate_manifest_index_alignment(manifest, index)
    manifest_bytes = canonical_json_bytes(manifest)
    index_bytes = canonical_json_bytes(index)
    manifest_offset = len(MAGIC) + HEADER_SIZE
    index_offset = manifest_offset + len(manifest_bytes)
    data_offset = index_offset + len(index_bytes)
    archive_size = data_offset + data_length

    header = {
        "container": FORMAT_NAME,
        "format_version": format_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "archive_id": manifest["archive_id"],
        "header_offset": len(MAGIC),
        "header_length": HEADER_SIZE,
        "manifest_offset": manifest_offset,
        "manifest_length": len(manifest_bytes),
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "index_offset": index_offset,
        "index_length": len(index_bytes),
        "index_sha256": sha256_bytes(index_bytes),
        "data_offset": data_offset,
        "data_length": data_length,
        "archive_size": archive_size,
        "entry_count": len(manifest["entries"]),
    }
    _validate_header(header)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as file_obj:
            file_obj.write(MAGIC)
            file_obj.write(_header_block(header))
            file_obj.write(manifest_bytes)
            file_obj.write(index_bytes)
            for chunk_id in chunk_offsets:
                file_obj.write(chunks[chunk_id])
            file_obj.flush()
            os.fsync(file_obj.fileno())
        if Path(temp_name).stat().st_size != archive_size:
            raise FormatError("archive size does not match the header")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return header


def write_archive_streaming(
    path: Path,
    manifest: dict,
    index: dict,
    chunk_files: dict[str, Path],
    *,
    buffer_size: int = 1024 * 1024,
    format_version: int = FORMAT_VERSION,
) -> dict:
    """Write an archive by copying chunk files with bounded memory.

    ``chunk_files`` contains already-compressed payloads. The binary layout is
    unchanged; only the publication path is streaming. ``format_version`` is 2
    when the manifest carries entry metadata a 1.x reader cannot honour.
    """

    if isinstance(buffer_size, bool) or not isinstance(buffer_size, int):
        raise FormatError("stream buffer size must be an integer")
    if buffer_size <= 0:
        raise FormatError("stream buffer size must be positive")
    path = Path(path)
    if manifest.get("archive_id") != index.get("archive_id"):
        raise FormatError("manifest and index archive_id values do not match")

    chunk_offsets: dict[str, int] = {}
    chunk_lengths: dict[str, int] = {}
    data_length = 0
    for chunk_id in dict.fromkeys(
        entry["chunk_id"] for entry in manifest["entries"]
    ):
        if chunk_id not in chunk_files:
            raise FormatError(f"missing data chunk: {chunk_id}")
        chunk_path = Path(chunk_files[chunk_id])
        if chunk_path.is_symlink() or not chunk_path.is_file():
            raise FormatError(f"invalid data chunk file: {chunk_path}")
        chunk_length = chunk_path.stat().st_size
        chunk_offsets[chunk_id] = data_length
        chunk_lengths[chunk_id] = chunk_length
        data_length += chunk_length

    for entry in manifest["entries"]:
        chunk_id = entry["chunk_id"]
        entry["chunk_offset"] = chunk_offsets[chunk_id]
        entry["chunk_length"] = chunk_lengths[chunk_id]
        entry["stored_size"] = chunk_lengths[chunk_id]

    validate_manifest(manifest, data_length=data_length)
    validate_index(index, expected_archive_id=manifest.get("archive_id"))
    validate_manifest_index_alignment(manifest, index)
    manifest_bytes = canonical_json_bytes(manifest)
    index_bytes = canonical_json_bytes(index)
    manifest_offset = len(MAGIC) + HEADER_SIZE
    index_offset = manifest_offset + len(manifest_bytes)
    data_offset = index_offset + len(index_bytes)
    archive_size = data_offset + data_length

    header = {
        "container": FORMAT_NAME,
        "format_version": format_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "archive_id": manifest["archive_id"],
        "header_offset": len(MAGIC),
        "header_length": HEADER_SIZE,
        "manifest_offset": manifest_offset,
        "manifest_length": len(manifest_bytes),
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "index_offset": index_offset,
        "index_length": len(index_bytes),
        "index_sha256": sha256_bytes(index_bytes),
        "data_offset": data_offset,
        "data_length": data_length,
        "archive_size": archive_size,
        "entry_count": len(manifest["entries"]),
    }
    _validate_header(header)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as file_obj:
            file_obj.write(MAGIC)
            file_obj.write(_header_block(header))
            file_obj.write(manifest_bytes)
            file_obj.write(index_bytes)
            for chunk_id in chunk_offsets:
                chunk_path = Path(chunk_files[chunk_id])
                remaining = chunk_lengths[chunk_id]
                with chunk_path.open("rb") as chunk_obj:
                    while remaining:
                        payload = chunk_obj.read(min(buffer_size, remaining))
                        if not payload:
                            raise FormatError(
                                f"data chunk changed while writing: {chunk_id}"
                            )
                        file_obj.write(payload)
                        remaining -= len(payload)
                    if chunk_obj.read(1):
                        raise FormatError(
                            f"data chunk changed while writing: {chunk_id}"
                        )
            file_obj.flush()
            os.fsync(file_obj.fileno())
        if Path(temp_name).stat().st_size != archive_size:
            raise FormatError("archive size does not match the header")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return header
