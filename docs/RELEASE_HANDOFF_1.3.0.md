# CloudArc 1.3.0 - release handoff

**Date:** 2026-09-11  **Author:** Viacheslav Bochkarev  **Scope:** core speed (measured), plus the 1.2.3 reliability fix

## 1. zstd for text (was: silent deflate fallback)

`zstandard` stays an *optional* accelerator - the CLI core keeps zero required
dependencies (it is baked into the packaged binary builds). What changed is that
a missing `zstandard` is no longer silent: `pack` reports
`warnings: ["zstandard is not installed: text payloads fall back to deflate..."]`
and the CLI prints it on stderr.

Measured on the CI text profile (64 MiB, `compressible-text`):

| | deflate | zstd |
|---|---|---|
| archive | 114 973 KiB | **18 461 KiB (6.2x smaller)** |
| pack | 2.88 s (22.2 MiB/s) | 2.40 s (26.6 MiB/s, +20%) |
| unpack | 0.33 s | 0.12 s (522 MiB/s) |

Measured on a real 2.2 GB mixed tree (`/root/vibo` + `~/.hermes`, `--no-index
--dedup`): archive 1.44 GB, saved 14.76%, 27.6 s, 87 MiB peak RSS - i.e. **no
size win at all**, because 3927 of 6333 files are already compressed (docx, pdf,
databases, binaries). Codecs used: store 3927, zstd 2402, deflate 4.

**Honest statement for any landing page:** speed and ratio depend on the data.
zstd helps text; it cannot help already-compressed media. The product's promise
is bounded memory and working on weak machines, not a compression ratio.

## 2. Process-pool packing (by files)

`pack(..., workers=N)` and `--workers N` (pack and push). Four phases: parallel
spool+hash, sequential chunk/dedup assignment in file order, parallel
compression of unique chunks, sequential assembly. Format, manifest, index,
entry order and dedup semantics are unchanged; `workers=1` is equivalent to the
old sequential packer (verified field by field, plus sha256 of every restored
file).

The pool is **not** the default - it is chosen by measurement. Automatic choice:

| input | workers=1 | workers=4 | speedup | peak tree RSS 1 -> 4 |
|---|---|---|---|---|
| 50 x 4 KiB (0.1 MiB) | 0.063 s | 0.083 s | **0.76x (pool loses)** | - |
| 2400 x 16 KiB (6.5 MiB) | 2.54 s | 2.22 s | 1.14x | - |
| 1000 x 16 KiB (16.4 MiB) | 2.59 s | 1.11 s | 2.33x | - |
| 4000 x 16 KiB (65.5 MiB) | 10.28 s | 4.50 s | 2.28x | 45 -> 117 MiB |
| 4 x 32 MiB (134 MiB) | 18.22 s | 5.13 s | 3.55x | 49 -> 155 MiB |
| 8 x 24 MiB (201 MiB) | 27.98 s | 7.58 s | 3.69x | - |

So the pool is used only above `POOL_MIN_TOTAL_BYTES` (16 MiB) and with at least
two files; below that the sequential path is faster. Peak tree RSS stays inside
the 256 MiB SLO (worst measured 155 MiB) and a warning is emitted whenever the
pool is used. Any pool failure (no ProcessPoolExecutor, no /dev/shm, fork
forbidden) falls back to sequential packing with a warning instead of failing.

## 3. Adaptive buffer - measured, rejected

The plan proposed raising the stream buffer to 8-16 MiB for small files,
promising +10-30%. Measured here it *slows the packer down*:

| input | buffer 1 MiB | buffer 8 MiB |
|---|---|---|
| 4000 x 16 KiB | 10.05 s | 10.74 s |
| 4 x 32 MiB | 18.10 s | 20.69 s |
| 1 x 64 MiB | 9.43 s | 9.81 s |

Not implemented. The 1 MiB buffer stays.

## 4. Also in this release (from the 1.2.3 work, never released separately)

A file that disappears between the directory walk and the read (SQLite removes
its `-shm`/`-wal` siblings, logs rotate) is now reported as `skipped: vanished`
instead of aborting the whole archive - found by backing up a live 2.2 GB tree,
where the pack died on `/root/.hermes/kanban.db-shm`.

## Verification

- `python3 -m unittest discover -s tests` -> **83 tests, OK** (1 skipped); 1.2.2 had 76.
  New: `tests/test_parallel_pack.py` (5 tests: worker resolution, equivalence of
  workers=1/2, the memory warning).
- `python3 tests/check_suite.py` -> inventory OK (11 files).
- CI: both matrix legs (CPython 3.12, zstandard without/with).
- Real run: 2.2 GB backup packed, restored, sha256 verified.

## Known limitations

- Parallelism only helps when there is real compression work: many small files
  or large files. A single huge file cannot be parallelised (one chunk).
- `benchmarks/large_package.py` has no `--workers` flag; the pool numbers above
  were measured through `core.packer.pack` directly.
- No cloud provider adapter exists yet (Google/Yandex are placeholders).
