# CloudArc `.vibo` Contract

**Status:** MVP contract, version 2 (backward-readable v1)  
**Date:** 2026-09-09  
**Owner:** Vibo CloudArc

## 1. Scope

CloudArc `.vibo` is a document archive container. It is intentionally
distinct from a ViBo memory graph (`memory.web` or a graph snapshot containing
`nodes` and `edges`). The portable reference backend implements this contract
without activating or licensing the supplied proprietary ViBo skill.

The optional native backend is accepted only after explicit capability
negotiation. A generic native `search` function is not treated as semantic
support.

## 2. Binary layout

All offsets are absolute byte offsets from the beginning of the file.

| Offset | Size | Section |
|---:|---:|---|
| `0` | 5 | ASCII magic `VIBO\n` |
| `5` | 8192 | UTF-8 JSON header, padded with ASCII spaces |
| `8197` | variable | Canonical UTF-8 manifest JSON |
| `manifest_end` | variable | Canonical UTF-8 versioned index JSON |
| `index_end` | variable | Concatenated compressed data chunks |

The fixed header makes the manifest/index discoverable with one small header
range request followed by two bounded section requests. Sidecars mirror the
two metadata sections for providers without reliable range reads.

### Range-read invariant

The metadata-only remote path reads exactly three ranges from the `.vibo`
object:

1. byte `0` through `len(MAGIC) + HEADER_SIZE - 1` (magic and fixed header);
2. `manifest_offset .. manifest_offset + manifest_length - 1`;
3. `index_offset .. index_offset + index_length - 1`.

The data section beginning at `data_offset` is never fetched by `list`,
`info` or `search`. Providers must return the requested byte count or fail;
silently accepting a truncated range would make the header and checksums
ambiguous.

## 3. Header

Required fields:

```json
{
  "container": "cloudarc",
  "format_version": 1,
  "created_at": "2026-09-07T00:00:00+00:00",
  "archive_id": "uuid",
  "header_offset": 5,
  "header_length": 8192,
  "manifest_offset": 8197,
  "manifest_length": 1234,
  "manifest_sha256": "hex",
  "index_offset": 2460,
  "index_length": 987,
  "index_sha256": "hex",
  "data_offset": 3447,
  "data_length": 4096,
  "archive_size": 7543,
  "entry_count": 3
}
```

Readers reject unknown container/format versions, invalid section ordering,
size mismatches and checksum failures.

## 4. Manifest

The current manifest schema is `cloudarc.manifest`, version `2`. Readers also
accept version `1` manifests that predate the explicit semantic-search
contract.

```json
{
  "schema": "cloudarc.manifest",
  "schema_version": 2,
  "archive_id": "uuid",
  "created_at": "2026-09-07T00:00:00+00:00",
  "tool": "cloudarc",
  "tool_version": "1.2.2",
  "dedup": true,
  "entry_count": 1,
  "raw_bytes": 1200,
  "methods": {"deflate": 1},
  "search": {
    "index_schema": "cloudarc.index",
    "index_schema_version": 2,
    "default_mode": "lexical",
    "lexical": {
      "schema": "cloudarc.lexical-index",
      "schema_version": 1,
      "tokenizer": "unicode-word-v1"
    },
    "semantic": {
      "schema": "cloudarc.semantic-index",
      "schema_version": 1,
      "backend": "native-vibo",
      "backend_api_version": "cloudarc.native-semantic-v1",
      "model_id": null,
      "dimensions": null,
      "metric": "cosine",
      "status": "unavailable",
      "index_ref": "none",
      "fallback": "lexical-v1",
      "reason": "portable-reference-backend; native semantic index not built"
    }
  },
  "entries": [
    {
      "entry_id": "e00000000",
      "path": "documents/readme.md",
      "size": 1200,
      "sha256": "hex",
      "media_type": "text/markdown",
      "requested_method": "zstd",
      "codec": "deflate",
      "stored_size": 440,
      "chunk_id": "c00000000-...",
      "chunk_offset": 0,
      "chunk_length": 440,
      "searchable": true
    }
  ]
}
```

### Path and integrity rules

- Paths are UTF-8 and normalized to POSIX separators.
- Paths must be relative.
- `..`, absolute paths, duplicate paths, empty paths and Windows drive-letter
  paths are invalid.
- Entry IDs and SHA-256 values are validated.
- Chunk offsets and lengths must remain inside the data section.
- The archive never stores the source machine's absolute path.

### Codec rules

| `requested_method` | Reference codec | Native-compatible intent |
|---|---|---|
| `zstd` | `zstd` when installed, otherwise `deflate` | high-ratio text |
| `webp_or_store` | `store` | preserve already encoded image bytes |
| `store` | `store` | archives, media and opaque binaries |

`codec` records what was actually written. `requested_method` records the
heuristic decision so results are auditable.

### Deduplication

When deduplication is enabled, entries with equal SHA-256 may share one
`chunk_id`. The later entry contains `dedup_of` pointing to the first entry.
Every logical path remains present in the manifest.

## 5. Versioned index

The current index schema is `cloudarc.index`, version `2`. Version `1` remains
readable through the same lexical search API.

```json
{
  "schema": "cloudarc.index",
  "schema_version": 2,
  "archive_id": "uuid",
  "lexical": {
    "schema": "cloudarc.lexical-index",
    "schema_version": 1,
    "tokenizer": "unicode-word-v1",
    "documents": {
      "e00000000": {
        "entry_id": "e00000000",
        "path": "documents/readme.md",
        "excerpt": "first searchable excerpt",
        "terms": ["cloudarc", "readme"]
      }
    },
    "postings": {
      "cloudarc": ["e00000000"]
    }
  },
  "semantic": {
    "schema": "cloudarc.semantic-index",
    "schema_version": 1,
    "backend": "native-vibo",
    "backend_api_version": "cloudarc.native-semantic-v1",
    "model_id": null,
    "dimensions": null,
    "metric": "cosine",
    "status": "unavailable",
    "index_ref": "none",
    "fallback": "lexical-v1"
  }
}
```

The lexical postings are built from the complete searchable text; `excerpt`
is only evidence returned to the caller and is capped at 2048 characters.

### Semantic capability contract

A native module must publish `CLOUDARC_CAPABILITIES` or a
`cloudarc_capabilities()` function containing:

```json
{
  "index_schema_versions": [2],
  "semantic_search": {
    "supported": true,
    "api_version": "cloudarc.native-semantic-v1",
    "search_function": "cloudarc_semantic_search",
    "descriptor_function": "cloudarc_semantic_descriptor",
    "model_ids": ["provider-defined"]
  }
}
```

The named function receives `(query, archive, limit)` or equivalent keyword
arguments and returns a list of result objects. The adapter rejects modules
that do not publish this exact versioned capability. Opaque native vector
bytes are not re-encoded by CloudArc.

The interpreter version is **not** part of the negotiation: the adapter probes a
real import, so any CPython with a matching build is accepted, and a failed
import reports which build tags exist and which one is required. When the module
publishes `descriptor_function`, CloudArc calls it at pack time to record
`model_id` and `dimensions` in the index descriptor; without it the index keeps
`status: unavailable` while semantic search still runs through the search
function.

### Search mode semantics

Every local or remote search response reports:

- `mode_requested`: what the caller asked for;
- `mode_used`: `semantic` only after native capability negotiation succeeds,
  otherwise `lexical`;
- `fallback_reason`: a stable explanation when lexical fallback was used.

Semantic fallback is explicit and never silently labels lexical results as
semantic.

## 6. Sidecars

For cloud use, the archive is uploaded with two sidecars:

```text
archive.vibo
archive.vibo.manifest.json
archive.vibo.index.json
```

Sidecars are pretty-printed JSON projections of the embedded sections. They
are regenerated from the embedded metadata in a temporary staging directory;
an existing user archive is never modified merely to create sidecars.

## 7. Streaming and bounded-memory behavior

The reference packer and unpacker are chunk-oriented:

- source files are copied, hashed and (when applicable) indexed from bounded
  byte buffers; the complete source file is never loaded into RAM;
- compression candidates are written to temporary payload files and the
  smaller raw/deflate/zstd candidate is selected before archive publication;
- the archive writer copies payload files in bounded chunks and verifies that
  a payload did not shrink or grow while it was being copied;
- unpack restores each entry through a streaming decoder directly into a
  staging file while calculating its SHA-256 and raw byte count;
- a source that changes during the spool pass is rejected rather than
  producing an archive whose digest describes a moving target.

This bounds payload memory by the configured stream buffer (1 MiB in the
reference implementation) plus manifest/index metadata and lexical postings.
Temporary disk usage can be larger than the final archive because raw and
candidate compressed payloads coexist until a codec is selected. The
compatibility helper `read_entry_bytes()` intentionally materializes one
entry for legacy callers; the normal `pack`, `unpack` and remote-search paths
do not use it.

The reference implementation publishes a bounded-memory acceptance profile for
large packages. For one file at 1, 5 and 10 GiB, the target is peak RSS of at
most 256 MiB per operation with no more than a 64 MiB peak-RSS spread across
sizes. Logical staging disk is capped at `2.10 * payload + 64 MiB` for pack
and `1.10 * payload + 64 MiB` for unpack. The measurement procedure,
workload identity and exclusions are normative in
`docs/LARGE_PACKAGE_SLO.md`.

## 8. Integrity and publication

- Readers verify the `VIBO` magic, header fields and section checksums.
- Unpack verifies every restored file size and SHA-256.
- A v2 manifest that references index v2 carries complete lexical and
  semantic metadata; legacy manifests still must cover every indexed document.
- Native semantic results are accepted only when any returned `entry_id` and
  `path` identify the same manifest entry.
- Archive and sidecars are staged and published together locally, with
  backups before replacement.
- Pull verifies the downloaded embedded manifest/index against the remote
  sidecars before restoring files.
- A push verifies all three remote objects and validates matching
  `archive_id`/schema versions before writing an `applied` action record.
- Symlink inputs and output targets are rejected before path resolution.
- Unknown format, manifest, index, semantic API versions and codecs fail
  closed.
- A partial upload is never advertised as a committed searchable archive.

## 9. Compatibility boundary

The portable reference backend is the compatibility baseline for CloudArc.
Native ViBo use is opt-in and requires an explicit capability handshake plus the
capability contract above. Native output that cannot produce this
manifest/index contract must be wrapped or rejected; it must not be silently
treated as a CloudArc archive.
