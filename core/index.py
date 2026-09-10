"""Versioned lexical and semantic-search index contract.

The portable backend only builds the lexical portion of the index.  A native
ViBo backend may advertise a compatible semantic API, but semantic support is
never inferred from the presence of a generic ``search`` function.
"""

from __future__ import annotations

import codecs
import re
from collections import defaultdict
from typing import Iterable

from .errors import FormatError


INDEX_SCHEMA = "cloudarc.index"
INDEX_SCHEMA_VERSION = 2
SUPPORTED_INDEX_SCHEMA_VERSIONS = {1, 2}

LEXICAL_SCHEMA = "cloudarc.lexical-index"
LEXICAL_SCHEMA_VERSION = 1

SEMANTIC_SCHEMA = "cloudarc.semantic-index"
SEMANTIC_SCHEMA_VERSION = 1
NATIVE_SEMANTIC_API_VERSION = "cloudarc.native-semantic-v1"

TOKENIZER = "unicode-word-v1"
MAX_EXCERPT_CHARS = 2048
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)
TRAILING_TOKEN_RE = re.compile(r"[^\W_]+$", flags=re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Return normalized Unicode word tokens."""

    return [token.lower() for token in TOKEN_RE.findall(text)]


class StreamingTextIndexer:
    """Collect searchable terms and a bounded excerpt from UTF-8 byte chunks."""

    def __init__(self, *, max_excerpt_chars: int = MAX_EXCERPT_CHARS):
        if (
            isinstance(max_excerpt_chars, bool)
            or not isinstance(max_excerpt_chars, int)
            or max_excerpt_chars < 0
        ):
            raise FormatError("max excerpt chars must be a non-negative integer")
        self.max_excerpt_chars = max_excerpt_chars
        self._decoder = codecs.getincrementaldecoder("utf-8")(
            errors="replace"
        )
        self._carry = ""
        self._terms: set[str] = set()
        self._excerpt_parts: list[str] = []
        self._excerpt_chars = 0
        self._finished = False

    def feed_bytes(self, payload: bytes) -> None:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise FormatError("streaming text payload must be bytes")
        if self._finished:
            raise FormatError("streaming text indexer is already finished")
        self.feed_text(self._decoder.decode(bytes(payload), final=False))

    def feed_text(self, text: str, *, final: bool = False) -> None:
        if self._finished:
            raise FormatError("streaming text indexer is already finished")
        if not isinstance(text, str):
            raise FormatError("streaming text payload must be text")
        if not text:
            if final and self._carry:
                self._terms.update(tokenize(self._carry))
                self._carry = ""
            if final:
                self._finished = True
            return

        if self._excerpt_chars < self.max_excerpt_chars:
            remaining = self.max_excerpt_chars - self._excerpt_chars
            excerpt = text[:remaining]
            self._excerpt_parts.append(excerpt)
            self._excerpt_chars += len(excerpt)

        combined = self._carry + text
        self._carry = ""
        if not final:
            trailing = TRAILING_TOKEN_RE.search(combined)
            if trailing is not None and trailing.end() == len(combined):
                self._carry = trailing.group(0)
                combined = combined[: trailing.start()]
        self._terms.update(tokenize(combined))
        if final:
            self._finished = True

    def finish(self) -> None:
        if self._finished:
            return
        self.feed_text(self._decoder.decode(b"", final=True), final=True)
        self._finished = True

    @property
    def excerpt(self) -> str:
        return "".join(self._excerpt_parts)[: self.max_excerpt_chars]

    @property
    def terms(self) -> list[str]:
        return sorted(self._terms)


def normalize_limit(limit: int) -> int:
    """Validate a bounded result limit used by local and remote search."""

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise FormatError("search limit must be an integer")
    if limit < 1 or limit > MAX_LIMIT:
        raise FormatError(
            f"search limit must be between 1 and {MAX_LIMIT}"
        )
    return limit


def unavailable_semantic_descriptor(
    *,
    reason: str = "portable-reference-backend",
) -> dict:
    """Return an explicit, non-claiming semantic capability descriptor."""

    return {
        "schema": SEMANTIC_SCHEMA,
        "schema_version": SEMANTIC_SCHEMA_VERSION,
        "backend": "native-vibo",
        "backend_api_version": NATIVE_SEMANTIC_API_VERSION,
        "model_id": None,
        "dimensions": None,
        "metric": "cosine",
        "status": "unavailable",
        "index_ref": "none",
        "fallback": "lexical-v1",
        "reason": reason,
    }


def ready_semantic_descriptor(
    *,
    model_id: str,
    dimensions: int,
    metric: str = "cosine",
    index_ref: str = "native",
) -> dict:
    """Build a validated descriptor for a native semantic index.

    The descriptor contains metadata only.  Opaque native vector bytes remain
    owned by the native backend and are never guessed or re-encoded here.
    """

    if not isinstance(model_id, str) or not model_id.strip():
        raise FormatError("semantic model_id must be a non-empty string")
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise FormatError("semantic dimensions must be an integer")
    if dimensions <= 0:
        raise FormatError("semantic dimensions must be positive")
    if metric not in {"cosine", "dot", "l2"}:
        raise FormatError(f"unsupported semantic metric: {metric}")
    if index_ref not in {"native", "embedded", "sidecar", "external"}:
        raise FormatError(f"unsupported semantic index_ref: {index_ref}")
    return {
        "schema": SEMANTIC_SCHEMA,
        "schema_version": SEMANTIC_SCHEMA_VERSION,
        "backend": "native-vibo",
        "backend_api_version": NATIVE_SEMANTIC_API_VERSION,
        "model_id": model_id,
        "dimensions": dimensions,
        "metric": metric,
        "status": "ready",
        "index_ref": index_ref,
        "fallback": "lexical-v1",
        "reason": None,
    }


def _normalize_document(document: dict) -> tuple[dict, str]:
    if not isinstance(document, dict):
        raise FormatError("index document must be an object")
    if "entry_id" not in document or "path" not in document:
        raise FormatError("index document requires entry_id and path")

    entry_id = str(document["entry_id"])
    path = str(document["path"])
    # ``content`` is used only while building the index and is never persisted.
    content = str(
        document.get("content", document.get("text", document.get("excerpt", "")))
    )
    excerpt = str(document.get("excerpt", content))[:MAX_EXCERPT_CHARS]
    provided_terms = document.get("terms")
    if provided_terms is None:
        terms = sorted(set(tokenize(f"{path} {content}")))
    else:
        if not isinstance(provided_terms, list) or any(
            not isinstance(term, str) for term in provided_terms
        ):
            raise FormatError("index document terms must be a string list")
        normalized_terms = set(tokenize(path))
        for term in provided_terms:
            normalized_terms.update(tokenize(term))
        terms = sorted(normalized_terms)
    normalized = {
        "entry_id": entry_id,
        "path": path,
        "excerpt": excerpt,
        "terms": terms,
    }
    return normalized, content


def build_index(
    documents: Iterable[dict],
    *,
    archive_id: str | None = None,
    semantic: dict | None = None,
) -> dict:
    """Build a v2 index while retaining a reader for v1 indexes.

    Each document may contain ``content``/``text`` for complete indexing and
    an optional short ``excerpt`` for evidence in search responses.  Complete
    searchable text is tokenized; only the excerpt is persisted.
    """

    normalized_docs: dict[str, dict] = {}
    postings: dict[str, set[str]] = defaultdict(set)

    for document in documents:
        normalized, _ = _normalize_document(document)
        entry_id = normalized["entry_id"]
        if entry_id in normalized_docs:
            raise FormatError(f"duplicate index entry_id: {entry_id}")
        normalized_docs[entry_id] = normalized
        for term in normalized["terms"]:
            postings[term].add(entry_id)

    lexical = {
        "schema": LEXICAL_SCHEMA,
        "schema_version": LEXICAL_SCHEMA_VERSION,
        "tokenizer": TOKENIZER,
        "documents": normalized_docs,
        "postings": {
            term: sorted(ids) for term, ids in sorted(postings.items())
        },
    }
    if semantic is None:
        semantic = unavailable_semantic_descriptor()
    # Validate the descriptor before serializing it into the archive.
    _validate_semantic_descriptor(semantic)

    result = {
        "schema": INDEX_SCHEMA,
        "schema_version": INDEX_SCHEMA_VERSION,
        "archive_id": archive_id,
        "lexical": lexical,
        "semantic": semantic,
    }
    if archive_id is None:
        result.pop("archive_id")
    validate_index(result)
    return result


def _validate_semantic_descriptor(descriptor: dict) -> None:
    if not isinstance(descriptor, dict):
        raise FormatError("semantic descriptor must be an object")
    if descriptor.get("schema") != SEMANTIC_SCHEMA:
        raise FormatError("unsupported semantic schema")
    if descriptor.get("schema_version") != SEMANTIC_SCHEMA_VERSION:
        raise FormatError("unsupported semantic schema version")
    if descriptor.get("backend") != "native-vibo":
        raise FormatError("unsupported semantic backend")
    if descriptor.get("backend_api_version") != NATIVE_SEMANTIC_API_VERSION:
        raise FormatError("unsupported semantic backend API version")
    if descriptor.get("status") not in {"unavailable", "ready"}:
        raise FormatError("invalid semantic status")
    if descriptor.get("metric") not in {"cosine", "dot", "l2"}:
        raise FormatError("invalid semantic metric")
    if descriptor.get("index_ref") not in {
        "none",
        "native",
        "embedded",
        "sidecar",
        "external",
    }:
        raise FormatError("invalid semantic index_ref")
    if descriptor.get("fallback") != "lexical-v1":
        raise FormatError("semantic fallback must be lexical-v1")
    if descriptor["status"] == "ready":
        model_id = descriptor.get("model_id")
        dimensions = descriptor.get("dimensions")
        if not isinstance(model_id, str) or not model_id.strip():
            raise FormatError("ready semantic index requires model_id")
        if (
            isinstance(dimensions, bool)
            or not isinstance(dimensions, int)
            or dimensions <= 0
        ):
            raise FormatError("ready semantic index requires positive dimensions")
        if descriptor.get("index_ref") == "none":
            raise FormatError("ready semantic index requires an index_ref")
    else:
        # An unavailable descriptor must not accidentally carry a claim of a
        # usable vector index.
        if descriptor.get("index_ref") != "none":
            raise FormatError(
                "unavailable semantic index must use index_ref=none"
            )
        if descriptor.get("model_id") is not None:
            raise FormatError(
                "unavailable semantic index cannot declare model_id"
            )
        if descriptor.get("dimensions") is not None:
            raise FormatError(
                "unavailable semantic index cannot declare dimensions"
            )


def _validate_lexical_block(lexical: dict) -> None:
    if not isinstance(lexical, dict):
        raise FormatError("lexical index must be an object")
    if lexical.get("schema") != LEXICAL_SCHEMA:
        raise FormatError("unsupported lexical schema")
    if lexical.get("schema_version") != LEXICAL_SCHEMA_VERSION:
        raise FormatError("unsupported lexical schema version")
    if lexical.get("tokenizer") != TOKENIZER:
        raise FormatError("unsupported lexical tokenizer")
    documents = lexical.get("documents")
    postings = lexical.get("postings")
    if not isinstance(documents, dict) or not isinstance(postings, dict):
        raise FormatError("lexical documents/postings must be objects")

    for entry_id, document in documents.items():
        if not isinstance(entry_id, str) or not isinstance(document, dict):
            raise FormatError("invalid lexical document")
        if document.get("entry_id") != entry_id:
            raise FormatError("lexical document entry_id mismatch")
        if not isinstance(document.get("path"), str):
            raise FormatError("lexical document path must be a string")
        if not isinstance(document.get("excerpt"), str):
            raise FormatError("lexical document excerpt must be a string")
        terms = document.get("terms")
        if not isinstance(terms, list) or any(
            not isinstance(term, str) for term in terms
        ):
            raise FormatError("lexical document terms must be a string list")

    for term, entry_ids in postings.items():
        if not isinstance(term, str) or not isinstance(entry_ids, list):
            raise FormatError("invalid lexical posting")
        if any(
            not isinstance(entry_id, str) or entry_id not in documents
            for entry_id in entry_ids
        ):
            raise FormatError("lexical posting references an unknown document")


def validate_index(
    index: dict,
    *,
    expected_archive_id: str | None = None,
) -> None:
    """Validate v1 or v2 index data before local or remote use."""

    if not isinstance(index, dict):
        raise FormatError("index must be an object")
    if index.get("schema") != INDEX_SCHEMA:
        raise FormatError("unsupported index schema")
    version = index.get("schema_version")
    if version not in SUPPORTED_INDEX_SCHEMA_VERSIONS:
        raise FormatError("unsupported index schema version")
    archive_id = index.get("archive_id")
    if (
        expected_archive_id is not None
        and archive_id is not None
        and archive_id != expected_archive_id
    ):
        raise FormatError("index archive_id does not match archive")
    if archive_id is not None and (
        not isinstance(archive_id, str) or not archive_id.strip()
    ):
        raise FormatError("index archive_id must be a non-empty string")

    if version == 1:
        lexical = {
            "schema": LEXICAL_SCHEMA,
            "schema_version": LEXICAL_SCHEMA_VERSION,
            "tokenizer": index.get("tokenizer"),
            "documents": index.get("documents"),
            "postings": index.get("postings"),
        }
        _validate_lexical_block(lexical)
        return

    if archive_id != expected_archive_id and expected_archive_id is not None:
        raise FormatError("v2 index archive_id does not match archive")
    _validate_lexical_block(index.get("lexical"))
    _validate_semantic_descriptor(index.get("semantic"))


def lexical_view(index: dict) -> dict:
    """Return a normalized lexical block for either supported index version."""

    validate_index(index)
    if index.get("schema_version") == 1:
        return {
            "schema": LEXICAL_SCHEMA,
            "schema_version": LEXICAL_SCHEMA_VERSION,
            "tokenizer": index["tokenizer"],
            "documents": index["documents"],
            "postings": index["postings"],
        }
    return index["lexical"]


def semantic_descriptor(index: dict) -> dict:
    """Return a semantic descriptor, including an explicit v1 fallback."""

    validate_index(index)
    if index.get("schema_version") == 1:
        return unavailable_semantic_descriptor(
            reason="legacy-index-without-semantic-contract"
        )
    return index["semantic"]


def search_index(index: dict, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    limit = normalize_limit(limit)
    if not isinstance(query, str):
        raise FormatError("search query must be a string")
    terms = sorted(set(tokenize(query)))
    if not terms:
        return []

    lexical = lexical_view(index)
    documents = lexical.get("documents", {})
    postings = lexical.get("postings", {})
    scores: dict[str, int] = defaultdict(int)
    matched: dict[str, list[str]] = defaultdict(list)

    for term in terms:
        for entry_id in postings.get(term, []):
            scores[entry_id] += 1
            matched[entry_id].append(term)

    results: list[dict] = []
    for entry_id, hit_count in scores.items():
        document = documents.get(entry_id, {})
        results.append(
            {
                "entry_id": entry_id,
                "path": document.get("path", ""),
                "excerpt": document.get("excerpt", ""),
                "score": round(hit_count / len(terms), 4),
                "matched_terms": sorted(matched[entry_id]),
            }
        )

    results.sort(key=lambda item: (-item["score"], item["path"]))
    return results[:limit]
