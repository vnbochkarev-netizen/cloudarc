# CloudArc Remote Search Protocol

**Protocol:** `cloudarc.remote-search`  
**Current version:** `1.1` (version `1.0` requests remain readable)  
**Date:** 2026-09-07

## 1. Goal

`list`, `info` and `search` must not download the archive body. CloudArc first
uses the embedded fixed header, manifest and index through provider range
reads. Providers without a reliable range API use the validated manifest and
index sidecars instead. Both paths execute the same request envelope and
validation rules.

## 2. Remote object set

For a remote archive path `P`, the provider stores:

```text
P
P.manifest.json
P.index.json
```

All three objects must be placed in the same weekly project folder.

Remote archive paths are normalized relative POSIX paths ending in `.vibo`.
Absolute paths, drive letters, backslashes and `..` are rejected.

## 3. Request envelope

```json
{
  "protocol": "cloudarc.remote-search",
  "protocol_version": "1.1",
  "operation": "search",
  "archive": "2026/SMARTBOT/неделя-01/CloudArc/docs.vibo",
  "query": "quarterly report",
  "mode": "semantic",
  "limit": 20
}
```

Allowed operations:

- `list`: return manifest entries;
- `info`: return archive metadata and search capability metadata;
- `search`: search the index sidecar.

`mode` is `lexical` or `semantic`; it defaults to `lexical`. A version `1.0`
request without `mode` is interpreted as lexical.

Limits are bounded to `1..100`. Invalid paths, modes, query types and limits
fail before any provider read.

## 4. Response envelopes

### `list`

```json
{
  "protocol": "cloudarc.remote-search",
  "protocol_version": "1.1",
  "operation": "list",
  "archive": "…",
  "entries": []
}
```

### `info`

```json
{
  "protocol": "cloudarc.remote-search",
  "protocol_version": "1.1",
  "operation": "info",
  "archive": "…",
  "archive_id": "uuid",
  "entry_count": 12,
  "raw_bytes": 2048,
  "methods": {"deflate": 8, "store": 4},
  "search": {
    "index_schema": "cloudarc.index",
    "index_schema_version": 2,
    "default_mode": "lexical",
    "semantic": {
      "backend": "native-vibo",
      "backend_api_version": "cloudarc.native-semantic-v1",
      "status": "unavailable",
      "fallback": "lexical-v1"
    }
  }
}
```

### `search`

```json
{
  "protocol": "cloudarc.remote-search",
  "protocol_version": "1.1",
  "operation": "search",
  "archive": "…",
  "query": "quarterly report",
  "mode": "lexical",
  "mode_requested": "semantic",
  "mode_used": "lexical",
  "fallback_reason": "remote-native-semantic-backend-unavailable",
  "semantic": {
    "schema": "cloudarc.semantic-index",
    "schema_version": 1,
    "backend": "native-vibo",
    "backend_api_version": "cloudarc.native-semantic-v1",
    "status": "unavailable",
    "fallback": "lexical-v1"
  },
  "results": [
    {
      "entry_id": "e00000000",
      "path": "reports/q1.md",
      "excerpt": "…",
      "score": 1.0,
      "matched_terms": ["quarterly", "report"]
    }
  ]
}
```

`mode` is a compatibility alias for `mode_used`. A response may report
`mode_used=semantic` only when an explicitly negotiated native semantic
executor handled the request. Otherwise lexical results are returned with a
non-empty `fallback_reason`.

Responses also include additive `telemetry` counters:

```json
{
  "range_requests": 3,
  "range_bytes": 12345,
  "sidecar_reads": 0,
  "sidecar_bytes": 0,
  "peak_rss_bytes": 25165824,
  "rss_source": "linux-proc-status",
  "mode_used": "lexical",
  "data_section_read": false
}
```

Embedded metadata normally produces three range requests (header, manifest,
index). Sidecar fallback produces two sidecar reads and zero range requests
after the provider reports unsupported range reads. `data_section_read` must
remain `false` for `list`, `info` and `search`.

## 5. Provider flow

1. `push --apply` validates the weekly project path.
2. CloudArc stages sidecars from the embedded archive metadata.
3. The provider uploads the `.vibo` object and both sidecars.
4. The provider confirms all three objects exist and the sidecars validate
   against one `archive_id` before the action is logged as `applied`.
5. `ls-cloud` reads only provider metadata.
6. `open list/info` confirms the archive object exists, then attempts the
   range path: byte range `0..8196` is parsed as the fixed header, followed by
   the manifest and index ranges named by that header.
7. If the provider reports `read_range` as unsupported, CloudArc reads and
   validates `P.manifest.json` and `P.index.json` instead. A real provider
   error is not silently converted into a sidecar fallback.
8. `fetch`/`open search` executes the index and returns lexical matches,
   or invokes a provider-native semantic executor when one is negotiated.
9. `pull --apply` downloads the archive body, verifies it matches both
   remote sidecars, and performs local hash-verified unpacking.

The local provider implements
`read_range(remote_path, offset, length)` and requires an exact-length
response. This is the reference contract for future HTTP adapters. The data
section beginning at `data_offset` is never requested by `open` or `fetch`.

## 6. Consistency and failure rules

- `archive_id` must match across the archive, manifest and index.
- Manifest/index schema versions must match the references in `manifest.search`.
- A v2 manifest referencing an index v2 must include complete lexical and
  semantic search metadata.
- Legacy manifests still must cover every document referenced by their index.
- Semantic result `entry_id`/`path` pairs must identify the same manifest
  entry.
- Sidecars from a previous upload must not be reused.
- A provider must not expose a partially uploaded archive as searchable.
- `fetch` never downloads the data section.
- `pull` is the only MVP operation that needs the complete archive body.
- Failed mutations are not reported as `applied`.

## 7. Security rules

- Remote paths are relative to the provider root and may not contain `..`,
  backslashes or drive letters.
- Cloud mutations are dry-run by default and require `--apply`.
- Local inputs and output targets that are symlinks are rejected before
  resolution.
- Uploads and `pull` are constrained to the configured weekly project folder.
- Tokens and OAuth material never appear in request or response envelopes.
- Action logs contain the provider, project and remote path, but not secrets.

## 8. Current implementation boundary

The protocol is fully implemented for the local filesystem provider and the
portable sidecar executor, including embedded range reads and sidecar
fallback. Yandex Disk and Google Drive remain explicit fail-closed
placeholders until token loading, OAuth refresh, upload confirmation and
provider-specific sidecar/range reads are added.
