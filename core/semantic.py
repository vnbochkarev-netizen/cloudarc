"""Explicit capability negotiation for the optional native ViBo backend.

The supplied ViBo package exposes a generic archive ``search`` entry point,
but that alone is not evidence that a CloudArc-compatible semantic index is
available.  This adapter accepts only a module that publishes the
``cloudarc.native-semantic-v1`` capability contract.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .errors import NativeBackendUnavailable
from .index import (
    INDEX_SCHEMA_VERSION,
    NATIVE_SEMANTIC_API_VERSION,
    normalize_limit,
)


DEFAULT_NATIVE_MODULE = "vibo_archive"


def interpreter_tag() -> str:
    """Build tag of the running interpreter: cpython-311, cpython-312, ..."""

    return f"cpython-{sys.version_info.major}{sys.version_info.minor}"


def _build_mismatch_hint(exc: Exception) -> str:
    """Tell "extension built for another CPython" apart from other import errors.

    Signals: the extension does not export ``PyInit_<name>`` for this
    interpreter, a wrong ELF class, or an incompatible platform.
    """

    text = f"{exc.__class__.__name__}: {exc}"
    markers = (
        "does not define module export function",
        "wrong ELF class",
        "invalid ELF header",
        "incompatible architecture",
        "undefined symbol",
        "cannot open shared object",
        "file too short",
    )
    if any(m in text for m in markers):
        return (f"native module was built for a different CPython; a "
                f"{interpreter_tag()} build is required (import said: {text[:160]})")
    return ""


def _available_build_tags(module_path: str | None, module_name: str) -> list[str]:
    """Which builds of this module sit next to it (cpython-311, cpython-312, ...)."""

    roots = [Path(module_path).expanduser()] if module_path else []
    roots += [Path(p) for p in sys.path if p]
    tags: list[str] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                name = entry.name
                if not name.startswith(module_name + "."):
                    continue
                for suffix in (".so", ".pyd", ".dylib"):
                    if name.endswith(suffix):
                        tag = name[len(module_name) + 1:-len(suffix)]
                        if tag:
                            tags.append(tag)
        except OSError:
            continue
    return sorted(set(tags))


def _import_module(
    module_path: str | None = None,
    module_name: str = DEFAULT_NATIVE_MODULE,
) -> tuple[ModuleType | None, str]:
    """Import a candidate module without permanently mutating ``sys.path``."""

    original_path = list(sys.path)
    cached = sys.modules.get(module_name)
    try:
        if module_path:
            sys.path.insert(0, str(Path(module_path).expanduser()))
            if cached is not None:
                # A path was given explicitly: the module must come from there.
                # Otherwise the sys.modules cache reports "available" for an
                # unrelated module that was imported earlier.
                sys.modules.pop(module_name, None)
        importlib.invalidate_caches()
        return importlib.import_module(module_name), ""
    except Exception as exc:
        if cached is not None and module_name not in sys.modules:
            sys.modules[module_name] = cached
        hint = _build_mismatch_hint(exc)
        if hint:
            return None, f"{module_name}: {hint}"
        tags = _available_build_tags(module_path, module_name)
        if tags:
            return None, (f"{module_name} is not built for {interpreter_tag()} "
                          f"(builds found in {module_path or 'sys.path'}: "
                          f"{', '.join(tags)}); a {interpreter_tag()} build is required")
        return None, f"{module_name} import failed: {exc}"
    finally:
        sys.path[:] = original_path


def _capability_payload(module: ModuleType) -> dict:
    provider = getattr(module, "cloudarc_capabilities", None)
    if callable(provider):
        try:
            value = provider()
        except Exception:
            return {}
    else:
        value = getattr(module, "CLOUDARC_CAPABILITIES", {})
    return value if isinstance(value, dict) else {}


def _semantic_capability(
    module: ModuleType,
    payload: dict,
) -> tuple[dict, str]:
    semantic = payload.get("semantic_search")
    if semantic is None:
        semantic = payload.get("semantic")
    if not isinstance(semantic, dict):
        return (
            {
                "supported": False,
                "api_version": None,
                "index_schema_versions": [],
                "search_function": None,
            },
            "native module has no explicit CloudArc semantic capability",
        )

    supported = semantic.get("supported") is True
    api_version = semantic.get("api_version")
    versions = semantic.get(
        "index_schema_versions",
        payload.get("index_schema_versions", []),
    )
    if not isinstance(versions, list):
        versions = []
    function_name = semantic.get(
        "search_function",
        "cloudarc_semantic_search",
    )
    fn = getattr(module, function_name, None)
    capability = {
        "supported": supported,
        "api_version": api_version,
        "index_schema_versions": versions,
        "search_function": function_name,
        "model_ids": semantic.get("model_ids", []),
        "descriptor_function": semantic.get("descriptor_function", ""),
    }
    if not supported:
        return capability, "native module explicitly disables semantic search"
    if api_version != NATIVE_SEMANTIC_API_VERSION:
        return capability, "native semantic API version is incompatible"
    if INDEX_SCHEMA_VERSION not in versions:
        return capability, "native backend does not support CloudArc index v2"
    if not callable(fn):
        return capability, "native semantic search function is missing"
    return capability, ""


def probe_native(
    module_path: str | None = None,
    *,
    module_name: str = DEFAULT_NATIVE_MODULE,
) -> dict:
    """Probe archive and semantic capabilities without activating the skill."""

    result = {
        "available": False,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform": sys.platform,
        "module_path": module_path or "",
        "module_name": module_name,
        "reason": "",
        "capabilities": {},
        "semantic_available": False,
        "semantic_reason": "",
    }
    # This used to be a hard refusal ("CPython 3.11 required") - a proxy for
    # "our .so is tagged cp311". It made the native module unusable on 3.12 (or
    # any other version) even when a matching build existed. The check is now
    # real: try the import and inspect the published capability.
    result["interpreter"] = interpreter_tag()
    module, reason = _import_module(module_path, module_name)
    if module is None:
        result["reason"] = reason
        result["semantic_reason"] = reason
        return result

    payload = _capability_payload(module)
    semantic, semantic_reason = _semantic_capability(module, payload)
    result["available"] = True
    result["reason"] = "native archive module imported"
    result["capabilities"] = {
        "archive": payload.get("archive", {}),
        "semantic_search": semantic,
    }
    result["semantic_available"] = not bool(semantic_reason)
    result["semantic_reason"] = semantic_reason
    return result


@dataclass
class NativeSemanticBackend:
    """Validated native semantic search adapter."""

    module: ModuleType
    capability: dict

    @property
    def available(self) -> bool:
        return True

    @property
    def api_version(self) -> str:
        return str(self.capability["api_version"])

    def search(
        self,
        archive_path: str | Path,
        query: str,
        limit: int,
    ) -> list[dict]:
        if not isinstance(query, str):
            raise NativeBackendUnavailable("semantic query must be a string")
        limit = normalize_limit(limit)
        function_name = self.capability["search_function"]
        function = getattr(self.module, function_name, None)
        if not callable(function):
            raise NativeBackendUnavailable(
                "native semantic search function is unavailable"
            )

        # Bind against the declared function signature before calling, so an
        # implementation error is not mistaken for a positional API mismatch.
        try:
            signature = inspect.signature(function)
        except (TypeError, ValueError):
            signature = None
        keyword_call = {
            "query": query,
            "archive": str(Path(archive_path)),
            "limit": limit,
        }
        try:
            if signature is None:
                raise TypeError
            signature.bind(**keyword_call)
        except TypeError:
            try:
                signature.bind(query, str(Path(archive_path)), limit)
            except TypeError as exc:
                raise NativeBackendUnavailable(
                    "native semantic function has an incompatible signature"
                ) from exc
            raw = function(query, str(Path(archive_path)), limit)
        else:
            raw = function(**keyword_call)

        if not isinstance(raw, list):
            raise NativeBackendUnavailable(
                "native semantic search must return a list"
            )
        normalized: list[dict] = []
        for item in raw[:limit]:
            if not isinstance(item, dict):
                raise NativeBackendUnavailable(
                    "native semantic result must be an object"
                )
            path = item.get("path")
            if not isinstance(path, str) or not path:
                raise NativeBackendUnavailable(
                    "native semantic result requires a path"
                )
            row = dict(item)
            row.setdefault("score", 0.0)
            row.setdefault("excerpt", "")
            row.setdefault("matched_terms", [])
            normalized.append(row)
        return normalized


def get_native_semantic_backend(
    module_path: str | None = None,
    *,
    module_name: str = DEFAULT_NATIVE_MODULE,
) -> NativeSemanticBackend | None:
    """Return a backend only when the full capability contract is present."""

    module, _ = _import_module(module_path, module_name)
    if module is None:
        return None
    payload = _capability_payload(module)
    capability, reason = _semantic_capability(module, payload)
    if reason:
        return None
    return NativeSemanticBackend(module=module, capability=capability)


def native_semantic_descriptor(module_path: str | None = None, *,
                               module_name: str = DEFAULT_NATIVE_MODULE) -> dict | None:
    """Semantic engine metadata published by the native module, if it offers any."""

    backend = get_native_semantic_backend(module_path, module_name=module_name)
    if backend is None:
        return None
    name = backend.capability.get("descriptor_function") or ""
    if not name:
        return None
    fn = getattr(backend.module, name, None)
    if not callable(fn):
        return None
    try:
        value = fn()
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, dict) else None


def require_native(module_path: str | None = None) -> None:
    status = probe_native(module_path)
    if not status["available"]:
        raise NativeBackendUnavailable(status["reason"])


def require_semantic(
    module_path: str | None = None,
    *,
    module_name: str = DEFAULT_NATIVE_MODULE,
) -> NativeSemanticBackend:
    backend = get_native_semantic_backend(
        module_path,
        module_name=module_name,
    )
    if backend is None:
        status = probe_native(module_path, module_name=module_name)
        raise NativeBackendUnavailable(
            status.get("semantic_reason")
            or "compatible native semantic backend is unavailable"
        )
    return backend
