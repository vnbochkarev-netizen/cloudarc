# CloudArc MVP Architecture

**Date:** 2026-09-09  
**Version:** 1.2.2 / manifest-schema-v2 / index-schema-v2 /
semantic-index-v2 / remote-search-v1.1 / large-package-slo-v1

## 1. Selected architecture

CloudArc is a separate orchestration layer around a versioned document
container. It does not treat a ViBo memory graph as a CloudArc archive.

```text
cloudarc.py
  |
  +-- core.packer -------- reference .vibo backend
  |       +-- core.format       fixed header + manifest/index + chunks
  |       +-- core.manifest    v2 contract, v1 reader
  |       +-- core.index       v2 lexical/semantic contract, v1 reader
  |       +-- core.semantic    native capability negotiation + adapter
  |       +-- core.squeeze_hints
  |       +-- core.safety      path, backup and confirmation policy
  |
  +-- cloud.base ---------- provider contract + sidecar/range reads
  |       +-- cloud.local       implemented local provider (tests, dry runs)
  |       +-- cloud.yandex      Yandex Disk REST v1 (OAuth token, HTTP ranges)
  |       +-- cloud.google      fail-closed P0 stub
  |       +-- cloud.protocol    remote-search v1.1 / v1.0 reader
  |
  +-- stats.savings ------- redacted byte-savings and action logs
  +-- config.py ----------- non-secret validated configuration
```

## 2. P0 decisions

1. `ТЗ_vibo_cloudarc.md` is the canonical requirements source.
2. CloudArc uses a CloudArc-specific `.vibo` container, not a memory graph.
3. The fixed header makes manifest/index range-readable.
4. New archives publish manifest v2 and index v2; v1 remains readable.
5. Index v2 separates complete lexical postings from an explicit semantic
   capability descriptor.
6. Native semantic search is enabled only by the
   `cloudarc.native-semantic-v1` capability contract. A generic native
   `search` function is insufficient.
7. Lexical search is the guaranteed baseline. Semantic requests expose
   `mode_requested`, `mode_used` and `fallback_reason`.
8. Remote metadata reads prefer embedded header/manifest/index ranges and
   fall back to validated sidecars only when the provider lacks range support.
9. Archive/sidecar publication is staged and verified before an `applied`
   action is recorded.
10. Cloud mutations are dry-run by default; `--apply` is mandatory.
11. Cloud mutations require `--apply`; without credentials the Yandex adapter
    fails closed with a message naming the environment variable, and Google Drive
    stays a placeholder until OAuth credentials exist.
12. The supplied commercial ViBo skill is not activated or licensed.

## 3. Defect-first hardening applied

- Reject output paths inside input trees.
- Reject unpack targets that contain or replace the source archive.
- Restore into a staging directory, then publish after all hashes pass.
- Atomically stage local archive and sidecar writes, with backups before
  replacement.
- Regenerate sidecars from embedded metadata instead of trusting stale files.
- Validate manifest/index schema, archive IDs, checksums, paths, chunk ranges,
  codecs and bounded remote-search limits.
- Require v2 semantic/lexical search metadata and validate legacy index
  documents against manifest entries.
- Index complete searchable text rather than only the first 2048 characters.
- Constrain `pull` to the configured weekly project folder.
- Verify a downloaded archive body against the remote manifest/index sidecars
  before unpacking.
- Reject symlink inputs and output targets before path resolution.
- Keep CLI `pack`/`unpack` preview-only unless `--apply`/`--yes` is supplied.
- Reject orphan sidecar searches when the remote `.vibo` object is absent.
- Add explicit provider-neutral sidecar reads and local-provider atomic copies.
- Stream source spool, compression candidates, archive publication and
  unpacked output through bounded buffers; reject a source that changes while
  it is being spooled.
- Keep remote range reads above the data section and verify exact lengths,
  section checksums and archive size before executing a search.
- Measure large-package peak RSS and staging disk in isolated child processes;
  the bounded-memory SLO is normative for the 1/5/10 GiB acceptance profile.

## 4. Large-package data path

The reference backend uses a temporary-file pipeline for payload bytes:

1. each source is copied in 1 MiB chunks while its SHA-256 and lexical terms
   are collected;
2. a raw spool is compared with streaming deflate/zstd candidates;
3. the selected payload file is copied into the v1 container in bounded
   chunks;
4. unpack streams the selected chunk through its decoder into a staging file,
   hashing output as it goes;
5. only after every entry passes size/hash/path checks is the staging tree
   published.

Payload RAM is therefore bounded by the stream buffer and codec state. The
manifest/index and lexical postings remain in memory because they are the
search contract; temporary disk is the intentional trade-off for large
payloads.

The acceptance benchmark is `python -B -m benchmarks.large_package --sizes-gib
1 5 10`. It records parent RSS samples plus the worker OS high-water mark and
uses an operation-derived staging-disk floor so short atomic phases are not
missed. The normative limits and exclusions are in
`docs/LARGE_PACKAGE_SLO.md`; large runs are intentionally excluded from the
regular unit-test suite.

The Windows/Python 3.14 acceptance run completed for 1, 5 and 10 GiB on
2026-09-09. Peak RSS stayed within 25.8-26.0 MiB for pack and 29.7-31.2 MiB
for unpack; the machine-readable evidence is in
`benchmarks/results/large-package-windows-2026-09-09.json`.

## 5. Runtime matrix

| Component | MVP status |
|---|---|
| Reference pack/unpack/list/info/search | Works on Python 3.11+ and Windows |
| Versioned manifest/index v2 | Implemented; v1 reader retained |
| Lexical remote sidecar search | Implemented for local provider |
| Native semantic negotiation | Adapter implemented; requires Linux CPython 3.11 and explicit capability |
| Optional zstd | Used when the package is installed |
| Native `vibo_archive` from supplied skill | Preflight only; no license activation |
| Yandex Disk | REST v1 adapter: list/meta/folder/upload/download/delete/find/range reads; token from env or file |
| Google Drive | Placeholder; fails closed |

## 6. Non-goals for this MVP

- Telegram bot mode.
- Incremental backups.
- Native vector generation when the supplied module does not publish the
  CloudArc semantic capability.
- Silent cloud deletion.
- Automatic credential discovery.
- Treating ViBo memory files as CloudArc document archives.

## 7. Test strategy

The standard-library test suite covers:

- format header, section checksums and legacy-v1 reads;
- pack/unpack round-trip SHA-256;
- deduplication and overwrite confirmation;
- full-text lexical indexing beyond the excerpt limit;
- semantic fallback and explicit native capability negotiation;
- remote path/limit/schema validation;
- local provider sidecar search without the archive body;
- local provider range-only remote search without sidecars or data reads;
- streaming pack/unpack of multi-buffer payloads, UTF-8 token boundaries and
  compatibility-reader bypass;
- protected paths, output/input overlap and safe unpack staging;
- benchmark resource monitors, bounded deflate expansion and large-package
  smoke coverage; the full 1/5/10 GiB profile is an explicit acceptance run;
- native runtime preflight.
