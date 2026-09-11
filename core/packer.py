"""Reference pack/unpack/analyze/search implementation for CloudArc."""

from __future__ import annotations

import hashlib
import inspect
import os
import shutil
import tempfile
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .version import VERSION
from .errors import CloudArcError, FormatError, SafetyError
from .format import (
    read_header,
    read_index,
    read_manifest,
    sidecar_paths,
    stream_entry_to_file,
    write_archive_streaming,
    write_sidecars,
)
from .index import (
    StreamingTextIndexer,
    build_index,
    semantic_descriptor,
    search_index,
    unavailable_semantic_descriptor,
)
from .manifest import (
    MANIFEST_SCHEMA,
    MANIFEST_SCHEMA_VERSION,
    validate_manifest_index_alignment,
)
from .models import CompressionHint, SourceFile
from .safety import (
    backup_existing,
    ensure_safe_input,
    ensure_within,
)
from .semantic import get_native_semantic_backend, probe_native
from .squeeze_hints import predict, zstd_available


TOOL_VERSION = VERSION
STREAM_BUFFER_SIZE = 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(
            root.resolve(strict=False)
        )
        return True
    except ValueError:
        return False


def _validate_pack_output(inputs: Iterable[str | Path], output_path: Path) -> None:
    """Reject an output path inside an input tree.

    Without this guard a second pack of ``source`` could ingest its previous
    archive and sidecars, silently changing the archive contents.
    """

    for raw_input in inputs:
        source = ensure_safe_input(Path(raw_input))
        if source == output_path or _is_within(source, output_path):
            raise SafetyError(
                f"output must not be inside an input path: {output_path}"
            )


def _skip_reason(path: Path) -> str | None:
    """Classify why a file cannot enter the archive (``None`` when it can).

    The reason is reported to the caller instead of vanishing silently: a backup
    that drops 200 of 556 files without saying so is worse than no backup.
    """

    try:
        ensure_safe_input(path)
    except SafetyError as exc:
        message = str(exc)
        if "protected path" in message:
            return "protected"
        if "system path" in message:
            return "system"
        return "unsafe"
    return None


def _discover_files(
    inputs: Iterable[str | Path],
) -> tuple[list[SourceFile], list[dict]]:
    """Collect packable files and the files skipped inside the given trees.

    An explicitly passed symlink is still rejected (fail-closed). A symlink found
    while walking a directory is now *skipped and reported* instead of aborting
    the whole directory, and protected subtrees are reported too.
    """

    discovered: list[SourceFile] = []
    skipped: list[dict] = []
    used_archive_paths: set[str] = set()

    for raw_input in inputs:
        raw_path = Path(raw_input).expanduser()
        if raw_path.is_symlink():
            raise SafetyError(f"symlink input is not allowed: {raw_path}")
        source = ensure_safe_input(raw_path)
        if not source.exists():
            raise FileNotFoundError(source)
        if source.is_file():
            candidates = [(source, source.name)]
        elif source.is_dir():
            root_name = source.name or "root"
            candidates = []
            for child in sorted(source.rglob("*")):
                archive_path = (
                    Path(root_name) / child.relative_to(source)
                ).as_posix()
                if child.is_symlink():
                    skipped.append({"path": archive_path, "reason": "symlink"})
                    continue
                if not child.is_file():
                    continue
                if not _is_within(source, child):
                    raise SafetyError(
                        f"input path escapes source directory: {child}"
                    )
                reason = _skip_reason(child)
                if reason is not None:
                    skipped.append({"path": archive_path, "reason": reason})
                    continue
                candidates.append((child, archive_path))
        else:
            raise SafetyError(f"unsupported input path: {source}")

        for child, archive_path in candidates:
            ensure_safe_input(child)
            if archive_path in used_archive_paths:
                raise SafetyError(f"duplicate archive path: {archive_path}")
            used_archive_paths.add(archive_path)
            discovered.append(SourceFile(child, archive_path))

    if not discovered:
        raise SafetyError("no files found to pack")
    return discovered, skipped


def _codec_warnings(wanted_zstd: bool) -> list[str]:
    """Explain a silent codec downgrade instead of hiding it."""

    if wanted_zstd and not zstd_available():
        return [
            "zstandard is not installed: text payloads fall back to deflate "
            "(slower and larger). Install it (pip install zstandard / the [zstd] "
            "extra) or use a packaged binary build."
        ]
    return []


def _skipped_summary(skipped: list[dict]) -> dict:
    """Count skipped files by reason for CLI output and JSON callers."""

    by_reason: dict[str, int] = {}
    for item in skipped:
        by_reason[item["reason"]] = by_reason.get(item["reason"], 0) + 1
    return {"count": len(skipped), "by_reason": by_reason}


def _entry_id(index: int) -> str:
    return f"e{index:08d}"


def _spool_source(
    source: Path,
    raw_path: Path,
    *,
    searchable: bool,
) -> tuple[int, str, dict | None]:
    """Copy a source into a temp file while hashing and indexing by chunks."""

    try:
        before = source.stat()
    except OSError as exc:
        raise FormatError(f"cannot stat source while packing: {source}") from exc
    digest = hashlib.sha256()
    raw_size = 0
    collector = StreamingTextIndexer() if searchable else None
    with source.open("rb") as source_obj, raw_path.open("wb") as raw_obj:
        while True:
            payload = source_obj.read(STREAM_BUFFER_SIZE)
            if not payload:
                break
            raw_obj.write(payload)
            digest.update(payload)
            raw_size += len(payload)
            if collector is not None:
                collector.feed_bytes(payload)
    try:
        after = source.stat()
    except OSError as exc:
        raise FormatError(f"cannot stat source after packing: {source}") from exc
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise SafetyError(f"source changed while packing: {source}")
    if collector is None:
        return raw_size, digest.hexdigest(), None
    collector.finish()
    return (
        raw_size,
        digest.hexdigest(),
        {
            "excerpt": collector.excerpt,
            "terms": collector.terms,
        },
    )


def _compress_file(raw_path: Path, payload_path: Path, codec: str) -> None:
    """Compress a temp file with bounded read/write buffers."""

    if codec == "deflate":
        compressor = zlib.compressobj(level=9)
    elif codec == "zstd":
        try:
            import zstandard as zstd  # type: ignore
        except Exception as exc:
            raise FormatError(
                "zstd entry requires the optional zstandard package"
            ) from exc
        try:
            compressor = zstd.ZstdCompressor(level=3).compressobj()
        except Exception as exc:
            raise FormatError("zstd streaming compressor is unavailable") from exc
    else:
        raise FormatError(f"unsupported streaming codec: {codec}")

    compress = getattr(compressor, "compress", None)
    flush = getattr(compressor, "flush", None)
    if not callable(compress) or not callable(flush):
        raise FormatError(f"{codec} streaming compressor is unavailable")

    with raw_path.open("rb") as source_obj, payload_path.open("wb") as target:
        while True:
            payload = source_obj.read(STREAM_BUFFER_SIZE)
            if not payload:
                break
            try:
                compressed = compress(payload)
            except Exception as exc:
                raise FormatError(f"{codec} compression failed") from exc
            if compressed:
                target.write(compressed)
        try:
            tail = flush()
        except Exception as exc:
            raise FormatError(f"{codec} compression flush failed") from exc
        if tail:
            target.write(tail)


def _select_streaming_payload(
    raw_path: Path,
    staging_dir: Path,
    stem: str,
    hint,
    *,
    zstd_ok: bool | None = None,
) -> tuple[Path, str]:
    """Choose a compressed temp payload only when it beats raw storage.

    ``zstd_ok`` lets a worker reuse the caller's verdict about the optional
    zstandard package: a forked or spawned process must not silently pick zstd
    when the caller has it unavailable (or patched out).  ``None`` probes the
    current interpreter, preserving the original sequential behaviour.
    """

    if hint.default_codec == "store":
        return raw_path, "store"

    if zstd_ok is None:
        zstd_ok = zstd_available()

    candidates = []
    if hint.requested_method == "zstd" and not zstd_ok:
        preferred = hint.default_codec
    else:
        preferred = "zstd" if hint.requested_method == "zstd" else hint.default_codec
    candidates.append(preferred)
    if preferred == "zstd":
        candidates.append("deflate")

    raw_size = raw_path.stat().st_size
    for codec in candidates:
        payload_path = staging_dir / f"{stem}.{codec}"
        payload_path.unlink(missing_ok=True)
        try:
            _compress_file(raw_path, payload_path, codec)
        except FormatError:
            payload_path.unlink(missing_ok=True)
            if codec == "zstd":
                continue
            raise
        if payload_path.stat().st_size < raw_size:
            raw_path.unlink(missing_ok=True)
            return payload_path, codec
        payload_path.unlink(missing_ok=True)
    return raw_path, "store"


def _stream_estimate(
    source_file: SourceFile,
    hint,
    staging_dir: Path,
    position: int,
) -> tuple[int, int, str]:
    raw_path = staging_dir / f"estimate-{position}.raw"
    try:
        raw_size, _digest, _document = _spool_source(
            source_file.source,
            raw_path,
            searchable=False,
        )
        payload_path, codec = _select_streaming_payload(
            raw_path,
            staging_dir,
            f"estimate-{position}",
            hint,
        )
        return raw_size, payload_path.stat().st_size, codec
    finally:
        for candidate in staging_dir.glob(f"estimate-{position}.*"):
            candidate.unlink(missing_ok=True)


def _spool_worker(source: str, raw_path: str, searchable: bool) -> dict:
    """Phase A worker: spool one source file into staging.

    Top-level and picklable; arguments are plain strings/bools so a process pool
    can ship them without touching module state.  A file that vanished mid-run is
    reported (``status == "vanished"``) instead of raising, keeping the
    sequential contract that such a file is skipped, not fatal.
    """

    try:
        raw_size, digest, text_info = _spool_source(
            Path(source),
            Path(raw_path),
            searchable=searchable,
        )
    except FileNotFoundError:
        return {"status": "vanished"}
    return {
        "status": "ok",
        "raw_size": raw_size,
        "digest": digest,
        "text_info": text_info,
    }


def _compress_worker(
    raw_path: str,
    staging_dir: str,
    stem: str,
    requested_method: str,
    default_codec: str,
    zstd_ok: bool,
) -> dict:
    """Phase C worker: compress one unique chunk, keeping codec selection.

    Rebuilds a hint from plain strings (not a pickled object) and forwards the
    parent's ``zstd_ok`` verdict to :func:`_select_streaming_payload`.
    """

    hint = CompressionHint(
        requested_method=requested_method,
        default_codec=default_codec,
        searchable=False,
        reason="packer-worker",
    )
    payload_path, codec = _select_streaming_payload(
        Path(raw_path),
        Path(staging_dir),
        stem,
        hint,
        zstd_ok=zstd_ok,
    )
    return {"payload_path": str(payload_path), "codec": codec}


POOL_MIN_TOTAL_BYTES = 16 * 1024 * 1024


def _resolve_workers(workers: int | None, files: list | None = None) -> int:
    """Resolve the worker budget.

    Explicit values win (``0`` and ``None`` mean "choose automatically").
    Automatic choice is measured, not assumed: a process pool pays off only when
    there is real compression work to spread. Measured on this machine (4 CPUs,
    zstd), peak RSS of the whole process tree:

    | input | workers=1 | workers=4 | speedup | tree RSS 1 -> 4 |
    |---|---|---|---|---|
    | 6.5 MiB, 2400 small text files | 2.54 s | 2.22 s | 1.14x | - |
    | 16.4 MiB, 1000 x 16 KiB | 2.59 s | 1.11 s | 2.33x | - |
    | 65.5 MiB, 4000 x 16 KiB | 10.28 s | 4.50 s | 2.28x | 45 -> 117 MiB |
    | 134 MiB, 4 x 32 MiB | 18.22 s | 5.13 s | 3.55x | 49 -> 155 MiB |
    | 201 MiB, 8 x 24 MiB | 27.98 s | 7.58 s | 3.69x | - |

    50 tiny files: 0.76x (the pool loses). So the pool is used only when the
    inputs exceed ``POOL_MIN_TOTAL_BYTES`` (16 MiB); below that the sequential
    path is faster. The pool raises peak tree RSS (up to ~155 MiB measured,
    still inside the 256 MiB SLO) - hence the warning whenever it is used.
    """

    if workers is None or workers == 0:
        if not files or len(files) < 2:
            return 1
        total_bytes = 0
        for item in files:
            try:
                total_bytes += item.source.stat().st_size
            except OSError:
                continue
        if total_bytes < POOL_MIN_TOTAL_BYTES:
            return 1
        return min(4, os.cpu_count() or 1)
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 0:
        raise ValueError("workers must be a non-negative integer")
    return workers


def _pool_fallback_warning(exc: BaseException) -> str:
    return (
        f"process pool unavailable ({type(exc).__name__}: {exc}); "
        "fell back to sequential packing"
    )


def _pool_capable() -> bool:
    """True when the packing hooks are the real, picklable implementations.

    A test or embedder that swaps ``_spool_source`` / ``_select_streaming_payload``
    for a mock exposes behaviour a child process cannot reproduce, so we keep the
    work in-process rather than silently ignoring their patch.
    """

    return inspect.isfunction(_spool_source) and inspect.isfunction(
        _select_streaming_payload
    )


def _run_ordered_pool(worker, arguments: list, workers: int) -> tuple[list, str | None]:
    """Run ``worker(*args)`` over ``arguments`` in order, via a process pool.

    Processes (not threads) are used because compression is CPU-bound.  Any
    infrastructure failure - ProcessPoolExecutor missing, no ``/dev/shm``, fork
    forbidden - falls back to the sequential path and returns a warning, so the
    caller never aborts on a pool problem.  An expected CloudArc failure raised
    by a worker still propagates, exactly as in the sequential loop.
    """

    try:
        from concurrent.futures import ProcessPoolExecutor
    except Exception as exc:  # pragma: no cover - stdlib import cannot fail
        return [worker(*args) for args in arguments], _pool_fallback_warning(exc)
    try:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(worker, *arguments[index])
                for index in range(len(arguments))
            ]
            results = [future.result() for future in futures]
    except CloudArcError:
        raise
    except Exception as exc:
        return [worker(*args) for args in arguments], _pool_fallback_warning(exc)
    return results, None


def analyze(inputs: Iterable[str | Path]) -> dict:
    files, skipped = _discover_files(inputs)
    vanished: list[dict] = []
    rows: list[dict] = []
    raw_total = 0
    packed_total = 0
    with tempfile.TemporaryDirectory(prefix="cloudarc-analyze-") as temp_dir:
        staging_dir = Path(temp_dir)
        for position, source_file in enumerate(files):
            hint = predict(source_file.source)
            try:
                raw_size, packed_size, codec = _stream_estimate(
                    source_file,
                    hint,
                    staging_dir,
                    position,
                )
            except FileNotFoundError:
                # A file can disappear between the walk and the read: SQLite
                # removes its -shm/-wal siblings, logs rotate, editors swap
                # files. Report it instead of failing the whole archive.
                vanished.append({"path": source_file.archive_path, "reason": "vanished"})
                continue
            raw_total += raw_size
            packed_total += packed_size
            rows.append(
                {
                    "path": source_file.archive_path,
                    "raw_bytes": raw_size,
                    "requested_method": hint.requested_method,
                    "estimated_codec": codec,
                    "estimated_packed_bytes": packed_size,
                    "estimated_saved_bytes": raw_size - packed_size,
                    "searchable": hint.searchable,
                    "reason": hint.reason,
                }
            )

    saved = raw_total - packed_total
    skipped = skipped + vanished
    return {
        "files": rows,
        "raw_bytes": raw_total,
        "estimated_packed_bytes": packed_total,
        "estimated_saved_bytes": saved,
        "estimated_saved_pct": round((saved / raw_total * 100) if raw_total else 0, 2),
        "skipped": skipped,
        "skipped_summary": _skipped_summary(skipped),
    }


def _commit_archive_bundle(
    staged_archive: Path,
    staged_manifest: Path,
    staged_index: Path,
    output_path: Path,
    *,
    apply: bool,
) -> tuple[Path, Path, Path]:
    """Publish archive and sidecars together as far as the filesystem allows."""

    targets = [
        output_path,
        *sidecar_paths(output_path),
    ]
    if any(target.exists() for target in targets) and not apply:
        raise SafetyError(
            f"refusing to overwrite existing archive bundle {output_path}; "
            "rerun with --apply"
        )

    backups: dict[Path, Path] = {}
    for target in targets:
        if target.exists():
            backup = backup_existing(target)
            if backup is not None:
                backups[target] = backup

    replaced: list[Path] = []
    pairs = [
        (staged_archive, output_path),
        (staged_manifest, sidecar_paths(output_path)[0]),
        (staged_index, sidecar_paths(output_path)[1]),
    ]
    try:
        for source, target in pairs:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            replaced.append(target)
    except Exception:
        # Best-effort rollback keeps a failed publication from leaving a
        # mixture of old and new metadata.  Backups are retained for audit.
        for target in reversed(replaced):
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        for target, backup in backups.items():
            try:
                shutil.copy2(backup, target)
            except OSError:
                pass
        raise
    return output_path, pairs[1][1], pairs[2][1]


def pack(
    inputs: Iterable[str | Path],
    output: str | Path,
    *,
    dedup: bool = False,
    dry_run: bool = False,
    apply: bool = False,
    index: bool = True,
    stats_writer=None,
    stats_context: dict | None = None,
    max_package_bytes: int | None = None,
    workers: int | None = None,
) -> dict:
    input_values = list(inputs)
    search_index = bool(index)
    output_input = Path(output).expanduser()
    if output_input.is_symlink():
        raise SafetyError(f"symlink output is not allowed: {output_input}")
    output_path = output_input.resolve()
    ensure_safe_input(output_path.parent)
    _validate_pack_output(input_values, output_path)
    files, skipped = _discover_files(input_values)
    worker_budget = _resolve_workers(workers, files)
    skipped_summary = _skipped_summary(skipped)
    raw_input_bytes = sum(item.source.stat().st_size for item in files)
    if dry_run:
        plan = analyze(input_values)
        plan.update(
            {
                "dry_run": True,
                "output": str(output_path),
                "dedup": dedup,
                "index": index,
                "requires_apply": not apply,
                "skipped": skipped,
                "skipped_summary": skipped_summary,
            }
        )
        if max_package_bytes and raw_input_bytes > max_package_bytes:
            plan["warning"] = (
                f"source is {raw_input_bytes} bytes, above configured limit "
                f"{max_package_bytes} bytes"
            )
        return plan
    if (
        max_package_bytes
        and raw_input_bytes > max_package_bytes
        and not apply
    ):
        raise SafetyError(
            f"source is {raw_input_bytes} bytes, above configured limit "
            f"{max_package_bytes} bytes; rerun with --apply"
        )

    entries: list[dict] = []
    warnings: list[str] = []
    wanted_zstd = False
    first_chunk_by_hash: dict[str, str] = {}
    first_entry_by_hash: dict[str, str] = {}
    index_documents: list[dict] = []
    raw_total = 0
    methods: dict[str, int] = {}
    pool_warnings: list[str] = []
    pool_used = False

    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=".cloudarc-pack-", dir=output_path.parent)
    )
    staged_archive = staging_dir / output_path.name
    try:
        pool_enabled = worker_budget > 1 and _pool_capable()

        # ---- Phase A: parallel spooling, hashing and text indexing ----
        spool_args: list[tuple] = []
        spool_meta: list[dict] = []
        status_by_position: dict[int, str] = {}
        for position, source_file in enumerate(files):
            if not source_file.source.exists():
                status_by_position[position] = "vanished"
                continue
            hint = predict(source_file.source)
            if hint.requested_method == "zstd":
                wanted_zstd = True
            searchable = bool(hint.searchable and search_index)
            raw_path = staging_dir / f"raw-{position:08d}.bin"
            spool_args.append((str(source_file.source), str(raw_path), searchable))
            spool_meta.append(
                {
                    "position": position,
                    "source_file": source_file,
                    "hint": hint,
                    "searchable": searchable,
                    "raw_path": raw_path,
                }
            )

        if pool_enabled and len(spool_args) > 1:
            spool_results, pool_warning = _run_ordered_pool(
                _spool_worker, spool_args, worker_budget
            )
            if pool_warning is None:
                pool_used = True
            else:
                pool_warnings.append(pool_warning)
        else:
            spool_results = [_spool_worker(*args) for args in spool_args]

        records: list[dict | None] = [None] * len(files)
        for meta, outcome in zip(spool_meta, spool_results):
            if outcome["status"] != "ok":
                status_by_position[meta["position"]] = "vanished"
                continue
            status_by_position[meta["position"]] = "packed"
            records[meta["position"]] = {
                **meta,
                "raw_size": outcome["raw_size"],
                "digest": outcome["digest"],
                "text_info": outcome["text_info"],
            }
        raw_total = sum(
            record["raw_size"] for record in records if record is not None
        )

        # Skipped files stay in the original file order.
        for position, source_file in enumerate(files):
            if status_by_position.get(position) == "vanished":
                skipped.append(
                    {"path": source_file.archive_path, "reason": "vanished"}
                )

        # ---- Phase B: sequential chunk_id / dedup assignment (file order) ----
        chunk_order: list[str] = []
        chunk_spec: dict[str, dict] = {}
        for position, source_file in enumerate(files):
            record = records[position]
            if record is None:
                continue
            digest = record["digest"]
            dedup_of = None
            if dedup and digest in first_chunk_by_hash:
                chunk_id = first_chunk_by_hash[digest]
                dedup_of = first_entry_by_hash[digest]
                record["raw_path"].unlink(missing_ok=True)
            else:
                chunk_id = f"c{len(chunk_order):08d}-{digest[:16]}"
                chunk_order.append(chunk_id)
                chunk_spec[chunk_id] = {
                    "raw_path": record["raw_path"],
                    "position": position,
                    "requested_method": record["hint"].requested_method,
                    "default_codec": record["hint"].default_codec,
                }
                if dedup:
                    first_chunk_by_hash.setdefault(digest, chunk_id)
                    first_entry_by_hash.setdefault(digest, _entry_id(position))
            record["chunk_id"] = chunk_id
            record["dedup_of"] = dedup_of

        # ---- Phase C: parallel compression of the unique chunks only ----
        zstd_ok = zstd_available()
        compress_args = [
            (
                str(chunk_spec[chunk_id]["raw_path"]),
                str(staging_dir),
                f"payload-{chunk_spec[chunk_id]['position']:08d}",
                chunk_spec[chunk_id]["requested_method"],
                chunk_spec[chunk_id]["default_codec"],
                zstd_ok,
            )
            for chunk_id in chunk_order
        ]
        if pool_enabled and len(compress_args) > 1:
            compress_results, pool_warning = _run_ordered_pool(
                _compress_worker, compress_args, worker_budget
            )
            if pool_warning is None:
                pool_used = True
            else:
                pool_warnings.append(pool_warning)
        else:
            compress_results = [_compress_worker(*args) for args in compress_args]

        chunk_files: dict[str, Path] = {}
        chunk_codecs: dict[str, str] = {}
        for chunk_id, outcome in zip(chunk_order, compress_results):
            chunk_files[chunk_id] = Path(outcome["payload_path"])
            chunk_codecs[chunk_id] = outcome["codec"]

        # ---- Phase D: sequential assembly in the original file order ----
        for position, source_file in enumerate(files):
            record = records[position]
            if record is None:
                continue
            chunk_id = record["chunk_id"]
            codec = chunk_codecs[chunk_id]
            payload_path = chunk_files[chunk_id]
            methods[codec] = methods.get(codec, 0) + 1
            entry = {
                "entry_id": _entry_id(position),
                "path": source_file.archive_path,
                "size": record["raw_size"],
                "sha256": record["digest"],
                "media_type": _guess_media_type(source_file.source),
                "requested_method": record["hint"].requested_method,
                "codec": codec,
                "stored_size": payload_path.stat().st_size,
                "chunk_id": chunk_id,
                "searchable": record["searchable"],
            }
            if record["dedup_of"]:
                entry["dedup_of"] = record["dedup_of"]
            entries.append(entry)

            text_info = record["text_info"]
            if record["searchable"] and text_info is not None:
                index_documents.append(
                    {
                        "entry_id": entry["entry_id"],
                        "path": source_file.archive_path,
                        "excerpt": text_info["excerpt"],
                        "terms": text_info["terms"],
                    }
                )

        warnings = list(_codec_warnings(wanted_zstd))
        if pool_used and worker_budget > 1:
            warnings.append(
                f"workers={worker_budget}: peak RSS budget grows with the pool"
            )
        for message in pool_warnings:
            if message not in warnings:
                warnings.append(message)

        skipped_summary = _skipped_summary(skipped)
        archive_id = str(uuid.uuid4())
        semantic = unavailable_semantic_descriptor(
            reason="portable-reference-backend; native semantic index not built"
        )
        index = build_index(
            index_documents,
            archive_id=archive_id,
            semantic=semantic,
        )
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "archive_id": archive_id,
            "created_at": _utc_now(),
            "tool": "cloudarc",
            "tool_version": TOOL_VERSION,
            "dedup": dedup,
            "entry_count": len(entries),
            "raw_bytes": raw_total,
            "entries": entries,
            "methods": methods,
            "search": {
                "index_schema": index["schema"],
                "index_schema_version": index["schema_version"],
                "default_mode": "lexical",
                "index_built": search_index,
                "lexical": {
                    "schema": "cloudarc.lexical-index",
                    "schema_version": 1,
                    "tokenizer": "unicode-word-v1",
                },
                "semantic": semantic,
            },
        }

        header = write_archive_streaming(
            staged_archive,
            manifest,
            index,
            chunk_files,
            buffer_size=STREAM_BUFFER_SIZE,
        )
        staged_manifest, staged_index = write_sidecars(
            staged_archive,
            manifest,
            index,
        )
        committed_archive, manifest_path, index_path = _commit_archive_bundle(
            staged_archive,
            staged_manifest,
            staged_index,
            output_path,
            apply=apply,
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    packed_bytes = committed_archive.stat().st_size
    saved_bytes = raw_total - packed_bytes
    result = {
        "archive": str(committed_archive),
        "manifest": str(manifest_path),
        "index": str(index_path),
        "archive_id": archive_id,
        "entry_count": len(entries),
        "raw_bytes": raw_total,
        "packed_bytes": packed_bytes,
        "saved_bytes": saved_bytes,
        "saved_pct": round((saved_bytes / raw_total * 100) if raw_total else 0, 2),
        "methods": methods,
        "header": header,
        "dedup": dedup,
        "index_built": search_index,
        "skipped": skipped,
        "skipped_count": skipped_summary["count"],
        "warnings": warnings,
        "workers": worker_budget,
        "skipped_by_reason": skipped_summary["by_reason"],
        "search": {
            "mode": "lexical",
            "index_built": search_index,
            "semantic_status": semantic["status"],
        },
    }
    if max_package_bytes and packed_bytes > max_package_bytes:
        result["warning"] = (
            f"archive is {packed_bytes} bytes, above configured limit "
            f"{max_package_bytes} bytes"
        )
    if stats_writer:
        stats_writer(
            {
                **(stats_context or {}),
                "operation": "pack",
                "archive": output_path.name,
                "raw_bytes": raw_total,
                "packed_bytes": packed_bytes,
                "saved_bytes": saved_bytes,
                "saved_pct": result["saved_pct"],
                "methods": methods,
                "skipped_count": skipped_summary["count"],
            }
        )
    return result


def _guess_media_type(path: Path) -> str:
    import mimetypes

    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def list_archive(archive: str | Path) -> list[dict]:
    manifest = read_manifest(Path(archive))
    return manifest["entries"]


def info_archive(archive: str | Path) -> dict:
    path = Path(archive)
    header = read_header(path)
    manifest = read_manifest(path)
    index = read_index(path)
    validate_manifest_index_alignment(manifest, index)
    return {
        "header": header,
        "archive_id": manifest["archive_id"],
        "entry_count": manifest["entry_count"],
        "raw_bytes": manifest["raw_bytes"],
        "packed_bytes": path.stat().st_size,
        "dedup": manifest.get("dedup", False),
        "methods": manifest.get("methods", {}),
        "search": manifest.get("search", {}),
        "index_schema_version": index.get("schema_version"),
        "semantic": semantic_descriptor(index),
    }


def search_archive_result(
    archive: str | Path,
    query: str,
    *,
    limit: int = 20,
    mode: str = "lexical",
    native_module_path: str | None = None,
    native_module_name: str = "vibo_archive",
) -> dict:
    """Search locally and report the requested and actually used mode."""

    if mode not in {"lexical", "semantic"}:
        raise FormatError(f"unsupported search mode: {mode}")
    archive_path = Path(archive).expanduser().resolve()
    manifest = read_manifest(archive_path)
    index = read_index(archive_path)
    lexical_results = search_index(index, query, limit)
    descriptor = semantic_descriptor(index)
    if mode == "lexical":
        return {
            "query": query,
            "mode_requested": "lexical",
            "mode_used": "lexical",
            "fallback_reason": None,
            "semantic": descriptor,
            "results": lexical_results,
        }

    backend = get_native_semantic_backend(
        native_module_path,
        module_name=native_module_name,
    )
    if backend is None:
        status = probe_native(
            native_module_path,
            module_name=native_module_name,
        )
        reason = status.get("semantic_reason") or (
            "compatible native semantic backend is unavailable"
        )
        if descriptor.get("status") == "ready":
            reason = "declared semantic index requires an unavailable native backend"
        return {
            "query": query,
            "mode_requested": "semantic",
            "mode_used": "lexical",
            "fallback_reason": reason,
            "semantic": descriptor,
            "results": lexical_results,
        }

    try:
        semantic_results = backend.search(archive_path, query, limit)
        path_by_id = {
            entry["entry_id"]: entry["path"] for entry in manifest["entries"]
        }
        id_by_path = {path: entry_id for entry_id, path in path_by_id.items()}
        for result in semantic_results:
            if result.get("path") not in id_by_path:
                raise FormatError(
                    "native semantic result references an unknown archive path"
                )
            entry_id = result.get("entry_id")
            if entry_id is not None:
                if entry_id not in path_by_id:
                    raise FormatError(
                        "native semantic result references an unknown entry_id"
                    )
                if path_by_id[entry_id] != result["path"]:
                    raise FormatError(
                        "native semantic result has a mismatched entry_id/path"
                    )
    except Exception as exc:
        return {
            "query": query,
            "mode_requested": "semantic",
            "mode_used": "lexical",
            "fallback_reason": f"native semantic search failed: {type(exc).__name__}",
            "semantic": descriptor,
            "results": lexical_results,
        }
    return {
        "query": query,
        "mode_requested": "semantic",
        "mode_used": "semantic",
        "fallback_reason": None,
        "semantic": descriptor,
        "results": semantic_results,
    }


def search_archive(
    archive: str | Path,
    query: str,
    *,
    limit: int = 20,
    mode: str = "lexical",
    native_module_path: str | None = None,
    native_module_name: str = "vibo_archive",
) -> list[dict]:
    """Backward-compatible list-returning search helper."""

    return search_archive_result(
        archive,
        query,
        limit=limit,
        mode=mode,
        native_module_path=native_module_path,
        native_module_name=native_module_name,
    )["results"]


def unpack(
    archive: str | Path,
    output_dir: str | Path,
    *,
    dry_run: bool = False,
    apply: bool = False,
) -> dict:
    archive_input = Path(archive).expanduser()
    if archive_input.is_symlink():
        raise SafetyError(f"symlink archive is not allowed: {archive_input}")
    archive_path = archive_input.resolve()
    output_input = Path(output_dir).expanduser()
    if output_input.is_symlink():
        raise SafetyError(f"symlink unpack output is not allowed: {output_input}")
    output_path = output_input.resolve()
    if output_path == archive_path or _is_within(output_path, archive_path):
        raise SafetyError(
            "unpack output must not contain or replace the source archive"
        )
    ensure_safe_input(output_path.parent)
    manifest = read_manifest(archive_path)
    index = read_index(archive_path)
    validate_manifest_index_alignment(manifest, index)
    if output_path.exists() and output_path.is_file():
        raise SafetyError(f"unpack output is a file: {output_path}")
    if dry_run:
        return {
            "dry_run": True,
            "archive": str(archive_path),
            "output": str(output_path),
            "entry_count": len(manifest["entries"]),
            "output_exists": output_path.exists(),
            "requires_apply": not apply,
        }

    if output_path.exists() and not apply:
        raise SafetyError(
            f"output path exists; rerun with --apply: {output_path}"
        )

    output_parent = output_path.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".cloudarc-unpack-", dir=output_parent)
    )
    backup: Path | None = None
    try:
        for entry in manifest["entries"]:
            target = ensure_within(staging, staging / entry["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as target_obj:
                stream_entry_to_file(
                    archive_path,
                    entry,
                    target_obj,
                    buffer_size=STREAM_BUFFER_SIZE,
                )

        if output_path.exists():
            backup = backup_existing(output_path)
            shutil.rmtree(output_path)
        staging.rename(output_path)
        staging = None  # type: ignore[assignment]
    except Exception:
        if backup is not None and not output_path.exists():
            try:
                shutil.copytree(backup, output_path)
            except OSError:
                pass
        raise
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "archive": str(archive_path),
        "output": str(output_path),
        "restored": len(manifest["entries"]),
    }
