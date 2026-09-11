# CloudArc Large-Package Bounded-Memory SLO

**Status:** normative MVP target  
**Revision date:** 2026-09-09  
**Benchmark command:** `python -B -m benchmarks.large_package --sizes-gib 1 5 10`

## Scope

This SLO applies to the portable CloudArc reference backend for:

- one logical input file;
- the deterministic `compressible-text` benchmark workload;
- payload sizes of 1 GiB, 5 GiB and 10 GiB (GiB = 1024^3 bytes);
- a bounded lexical vocabulary and the normal streaming `pack`/`unpack` path;
- one fresh child process per operation.

The benchmark is deliberately separate from the regular unit-test suite. The
suite uses small smoke payloads; this document governs the large-package
acceptance run.

## Limits

| Measurement | Pack | Unpack |
|---|---:|---:|
| Peak worker RSS | <= 256 MiB | <= 256 MiB |
| Peak RSS spread across 1/5/10 GiB | <= 64 MiB | <= 64 MiB |
| Peak logical staging disk | <= 2.10 x payload + 64 MiB | <= 1.10 x payload + 64 MiB |

The RSS limit is an absolute cap. The spread limit is the scaling guard: a
payload increase must not produce a linear RSS increase. The temporary-disk
limits cover logical file bytes, not filesystem allocation units.

## Measurement method

1. The dataset is generated in 1 MiB writes; the complete payload is never
   materialized in the benchmark process.
2. `pack` and `unpack` each run in a new child process with
   `PYTHONDONTWRITEBYTECODE=1`.
3. The parent samples current RSS and staging directories at a fixed interval.
   The worker also reports the OS high-water mark (`PeakWorkingSetSize` on
   Windows, `VmHWM` on Linux). The recorded peak is the maximum of both.
4. Temporary disk is the maximum sampled logical size of matching
   `.cloudarc-pack-*` or `.cloudarc-unpack-*` directories, combined with an
   operation-derived floor. The floor captures short-lived atomic publication
   phases that a periodic sample could miss.
5. Source, durable archive, published sidecars and published unpack output are
   excluded from the sampled staging total. The operation floor accounts for
   the raw spool plus compressed payload, and for the compressed payload plus
   archive/sidecar publication where applicable.
6. A run is valid only when the worker reports successful JSON, the restored
   byte count matches the input size, all requested sizes completed, and every
   per-operation SLO check passes.
7. JSON and Markdown checkpoints are atomically refreshed after every completed
   size. An interrupted acceptance run therefore remains explicitly
   `in_progress` and preserves all completed measurements.
8. `--resume` may continue such a checkpoint only when requested sizes,
   sampling interval, timeout, profile, workload fingerprint and SLO are exact
   matches. The OS/Python environment and recomputed per-operation SLO results
   must also match. Completed sizes are not rerun because every size already
   used fresh pack and unpack worker processes.

## Workload identity

The report records the workload profile version, generator buffer size and a
SHA-256 fingerprint of the deterministic seed block. A change to the workload
profile requires a new benchmark report and must not be compared as if it were
the same run.

## Exclusions and known bounds

- Manifest/index and lexical-postings memory is metadata memory; it is not
  claimed to be independent of the number of files or unique terms.
- The SLO does not cover a native semantic backend, vector generation, cloud
  transfer buffers, provider SDK caches or network retry queues.
- `read_entry_bytes()` is a compatibility helper that intentionally materializes
  one entry; it is outside the production pack/unpack and remote-search path.
- RSS is resident working set/high-water mark, not a complete private-commit
  or cgroup-memory accounting model.
- Temporary disk is logical byte length, not allocated filesystem blocks.
- The operation-derived disk floor is validated for the single-file benchmark
  scope and must be revisited for multi-file or deduplicated workloads.

## Failure policy

`evaluation.pass` is true only when all requested sizes (1, 5 and 10 GiB) have
successful runs and both operations satisfy their absolute and scaling limits.
Insufficient free disk, timeout, worker failure, truncated output, invalid
measurements or an unverified size are failures for release acceptance; they
must not be converted into a passing result by relaxing the SLO.

## Multi-file cardinality profile

`python -B -m benchmarks.large_package --multi-file` is a separate acceptance
profile. It creates a deterministic directory tree, packages with
`dedup=True`, and records manifest/index cardinality explicitly. CI uses 512
files; manual runs may raise the count.

| Measurement | Limit |
|---|---:|
| Pack/unpack peak RSS | <= 512 MiB |
| Manifest entries | <= 10,000 |
| Lexical index documents | <= 10,000 |
| Temporary disk | measured and reported per operation |

This RSS limit includes metadata structures that grow with file and term
cardinality. It is not an O(1) claim for arbitrary manifests. Reports include
duplicate count, deduplication ratio, manifest entries, index documents and
index terms.

**Measured on a heterogeneous tree (2026-09-11, Linux x86_64, Python 3.11).**
The 26-28 MiB figures describe single-stream payloads (one 256 MiB binary: 26.6
MiB peak). A directory of many text/office files keeps the lexical index in
memory, so peak RSS tracks the textual payload: 64.1 MiB / 358 files -> 169 MiB,
137 MiB of text -> 99 MiB, 18 MiB of docx/pdf / 243 files -> 137 MiB. Packing
that 64.1 MiB tree with `--no-index` peaks at 29.7 MiB - the archive, the
payload pipeline and the restore are unchanged, only search is empty. Treat the
256 MiB SLO as a *streaming* claim: on text-heavy trees either pass `--no-index`
or budget 1-3x the textual payload.

## Verification

The acceptance profile passed on 2026-09-09 using Windows 11, AMD64 and Python
3.14.0. The authoritative machine-readable result is
`benchmarks/results/large-package-windows-2026-09-09.json`; the readable report
is `docs/LARGE_PACKAGE_BENCHMARK.md`.

| Measurement | Pack | Unpack |
|---|---:|---:|
| Peak RSS range, 1/5/10 GiB | 25.8-26.0 MiB | 29.7-31.2 MiB |
| Peak RSS spread | 0.2 MiB | 1.5 MiB |
| Peak staging ratio | 1.002 x | 1.000 x |
| SLO result | PASS | PASS |
