"""Compatibility facade for the optional native ViBo runtime.

The implementation lives in :mod:`core.semantic` so archive and semantic
capabilities are negotiated consistently.  This module keeps the original
``probe_native``/``require_native`` imports stable for callers.
"""

from .semantic import (
    DEFAULT_NATIVE_MODULE,
    NativeSemanticBackend,
    get_native_semantic_backend,
    probe_native,
    require_native,
    require_semantic,
)

__all__ = [
    "DEFAULT_NATIVE_MODULE",
    "NativeSemanticBackend",
    "get_native_semantic_backend",
    "probe_native",
    "require_native",
    "require_semantic",
]
