"""Portable compression and search hints.

The original specification references ``vibo_squeeze.py``.  That module is
not present in the supplied ViBo skill package, so CloudArc keeps the policy
in this small, testable adapter.  The requested method is recorded in the
manifest even when the portable backend uses a fallback codec.
"""

from __future__ import annotations

import zlib
from pathlib import Path

from .models import CompressionHint


TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".csv",
    ".log",
    ".py",
    ".xml",
    ".html",
    ".htm",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
}

ALREADY_COMPRESSED_EXTENSIONS = {
    ".zip",
    ".7z",
    ".gz",
    ".bz2",
    ".xz",
    ".rar",
    ".pdf",
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".mp3",
    ".wav",
    ".flac",
    ".iso",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".heic"}


def is_text_candidate(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS


def predict(path: Path) -> CompressionHint:
    """Return the requested policy for a path.

    ``zstd`` remains the requested text method so a native backend can honor
    it later.  The reference backend falls back to zlib/deflate.
    """

    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return CompressionHint(
            requested_method="zstd",
            default_codec="deflate",
            searchable=True,
            reason="text-like extension",
        )
    if suffix in IMAGE_EXTENSIONS:
        return CompressionHint(
            requested_method="webp_or_store",
            default_codec="store",
            searchable=False,
            reason="image is already encoded; preserve bytes",
        )
    if suffix in ALREADY_COMPRESSED_EXTENSIONS:
        return CompressionHint(
            requested_method="store",
            default_codec="store",
            searchable=False,
            reason="already compressed or opaque media",
        )
    return CompressionHint(
        requested_method="store",
        default_codec="store",
        searchable=False,
        reason="unknown binary type; avoid speculative recompression",
    )


def zstd_available() -> bool:
    """True when the optional ``zstandard`` package can be imported.

    The CLI core stays dependency-free; zstandard is an optional accelerator
    (and is baked into the packaged binary builds). When it is missing, text
    payloads fall back to deflate - which is reported to the caller instead of
    happening silently.
    """

    try:
        import zstandard  # noqa: F401  # type: ignore
    except Exception:
        return False
    return True


def compress_portable(raw: bytes, hint: CompressionHint) -> tuple[bytes, str]:
    """Compress bytes with the portable reference codec.

    If the optional ``zstandard`` package is installed, text uses zstd.
    Otherwise zlib/deflate is used and the manifest retains the requested
    method so a native implementation can be substituted later.
    """

    if not raw or hint.default_codec == "store":
        return raw, "store"

    if hint.requested_method == "zstd":
        try:
            import zstandard as zstd  # type: ignore

            payload = zstd.ZstdCompressor(level=3).compress(raw)
            if len(payload) < len(raw):
                return payload, "zstd"
        except Exception:
            pass

    payload = zlib.compress(raw, level=9)
    if len(payload) < len(raw):
        return payload, "deflate"
    return raw, "store"


def estimate_bytes(raw: bytes, hint: CompressionHint) -> tuple[int, str]:
    """Estimate packed bytes without changing the source."""

    payload, codec = compress_portable(raw, hint)
    return len(payload), codec
