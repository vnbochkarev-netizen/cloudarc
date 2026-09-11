# Vibo CloudArc MVP

![CloudArc SLO](docs/cloudarc-slo-badge.svg)

![Peak RSS vs payload size](docs/cloudarc-rss-vs-size.png)


CloudArc is a safety-first CLI around a CloudArc-specific `.vibo` container.
The repository includes a portable reference backend so local packing,
versioned manifest/index generation, round-trip restore, and remote-search
protocol tests work on Windows without the Linux ViBo native extension.

## Runtime

- Python 3.11 or newer for the reference backend.
- The supplied ViBo native extension is Linux CPython 3.11 only. Use
  `python cloudarc.py runtime` to see whether it is available.

## Quick start

```powershell
python cloudarc.py analyze .\documents
python cloudarc.py pack .\documents -o .\out\documents.vibo --apply
python cloudarc.py info .\out\documents.vibo
python cloudarc.py list .\out\documents.vibo
python cloudarc.py search .\out\documents.vibo "quarterly report"
python cloudarc.py search .\out\documents.vibo "quarterly report" --mode semantic
python cloudarc.py unpack .\out\documents.vibo -o .\restored --apply
```

Semantic requests negotiate the optional native ViBo capability contract. If a
compatible native backend is unavailable, the command returns lexical results
with `mode_requested=semantic`, `mode_used=lexical`, and a fallback reason.
The native module is never treated as semantic merely because it exposes a
generic `search` function.

New archives publish manifest/index schema v2 and the remote-search protocol
v1.1; legacy v1 metadata remains readable and is checked for manifest/index
entry alignment.

`pack` and `unpack` are preview-only unless `--apply`/`--yes` is supplied.
Overwriting an existing archive creates a sibling backup. Restoring into a
non-empty directory also requires `--apply`.

Large packages use a bounded-buffer pipeline: source files are spooled and
hashed in 1 MiB chunks, compression candidates stay on temporary disk, and
unpack streams each entry into a staging file while checking its size and
SHA-256. The search index and manifest are metadata and may be materialized in
memory; payload bytes are not. The lexical index *does* grow with the amount of
text you pack - see the measurements and the `--no-index` escape hatch in
[Known limitations](#known-limitations-and-honest-status).

The large-package acceptance benchmark measures peak RSS and logical staging
disk in isolated child processes:

```powershell
python -B -m benchmarks.large_package --sizes-gib 1 5 10 `
  --json-output benchmarks\results\large-package-windows-2026-09-09.json `
  --markdown-output docs\LARGE_PACKAGE_BENCHMARK.md `
  --fail-on-slo
```

The normative bounded-memory limits, measurement rules and exclusions are in
`docs/LARGE_PACKAGE_SLO.md`. The full GiB run is intentionally separate from
the regular unit-test suite; a small smoke profile is used for tests.
If the process is externally interrupted, rerun the same command with
`--resume`; exact-matching completed sizes are retained from the atomic
checkpoint.

Cloud mutations are dry-run by default. A real local-provider upload requires:

```powershell
python cloudarc.py push .\out\documents.vibo --disk local --apply
python cloudarc.py ls-cloud --disk local
python cloudarc.py fetch "2026/SMARTBOT/неделя-01/CloudArc/documents.vibo" "quarterly"
python cloudarc.py fetch "2026/SMARTBOT/неделя-01/CloudArc/documents.vibo" "quarterly" --mode semantic
python cloudarc.py pull "2026/SMARTBOT/неделя-01/CloudArc/documents.vibo" --disk local -o .\pulled --apply
```

`fetch` and `open list/info/search` first try embedded range reads of the
fixed header, manifest and index. They never read the data section. If a
provider does not implement range reads, CloudArc falls back to validated
`.manifest.json` and `.index.json` sidecars.

Yandex Disk and Google Drive adapters are explicit P0 placeholders. They fail
closed until token loading, OAuth refresh, and provider-specific range/sidecar
reads are implemented. No cloud credentials or providers are connected in this
MVP.

## Repository layout

```text
cloudarc.py                 CLI entry point
config.py                   validated defaults/config loading
benchmarks/                 reproducible RSS and staging-disk benchmark
core/                       format, manifest/index v2, semantic adapter, packer, safety
cloud/                      provider contract, local provider, P0 stubs
stats/                      redacted savings and action logs
docs/                       architecture and protocol contracts
tests/                      standard-library unit/integration tests
```

The format contract is in `docs/VIBO_CONTRACT.md`. The remote search protocol
is in `docs/REMOTE_SEARCH_PROTOCOL.md`. The selected architecture, defect-first
hardening and P0 decisions are in `docs/ARCHITECTURE.md`.

## Benchmark helper

`skills/cloudarc-bounded-memory-benchmark/scripts/cloudarc_benchmark.py` drives
the same profiles with environment checks and a badge renderer:

```powershell
# environment + repository readiness (no checkout required)
python -B skills\cloudarc-bounded-memory-benchmark\scripts\cloudarc_benchmark.py doctor
# smallest proof: 2 MiB pack/unpack with real RSS numbers
python -B skills\cloudarc-bounded-memory-benchmark\scripts\cloudarc_benchmark.py selfcheck --repo . --size-mib 2
# flat SVG SLO badge from a result artifact
python -B skills\cloudarc-bounded-memory-benchmark\scripts\cloudarc_benchmark.py badge --input benchmarks\results\ci-smoke.json --output docs\cloudarc-slo-badge.svg
python -B skills\cloudarc-bounded-memory-benchmark\scripts\cloudarc_benchmark.py smoke --repo .
```

`selfcheck` fails closed with the `doctor` report when the repository is missing,
so a reader who only has the skill gets an explanation instead of an import error.

## Known limitations and honest status

- **Peak RSS on a heterogeneous tree is not the 26-28 MiB number (measured).**
  That figure holds for single-stream payloads: packing one 256 MiB binary here
  cost 26.6 MiB peak RSS. A *directory* of text and office files behaves
  differently, because the lexical search index is materialized in memory: a
  358-file / 64.1 MiB tree peaked at **169 MiB**, a 137 MiB text tree at 99 MiB,
  and 18 MiB of docx/pdf (243 files) at 137 MiB. Budget roughly 1-3x the textual
  payload unless you opt out:
  ```bash
  python3 cloudarc.py pack ./tree -o tree.vibo --apply --no-index   # 29.7 MiB peak on the same 64.1 MiB tree
  ```
  `--no-index` produces the same archive and restores identically; it stores an
  empty index, so `search` returns nothing (the manifest records
  `search.index_built = false`, and the sidecars are ~9 points smaller).
- **Skipped files are reported, never silent (1.2.1).** Protected subtrees
  (`.git`, `__pycache__`, `.venv`, `node_modules`), symlinks found while walking a
  directory, and leading system paths are not packed. `pack`/`analyze` now return
  `skipped`, `skipped_count` and `skipped_by_reason`, and the CLI prints a
  `warning: N file(s) were not packed (...)` line on stderr (suppressed by
  `--json`). Before 1.2.1 that information did not exist: a 556-file tree
  produced a 356-file archive with no signal at all.
- **`bin/`, `var/`, `etc/` are names, not system paths (1.2.1).** Only a
  *leading* system directory (`/etc/...`, `/usr/...`, `/var/...`, `/proc/...`)
  is refused. A project tree containing `bin/cli.js`, `var/cache.txt` or
  `app/etc/config.yml` is packable; those files used to be dropped silently,
  which cost any Node or Python CLI project its `bin/` directory.
- **One symlink no longer aborts a whole directory (1.2.1).** A symlink found
  inside a directory is skipped and reported with reason `symlink`; a symlink
  passed explicitly on the command line is still refused (fail-closed), and
  symlinks are never dereferenced.

- **Where were these numbers measured?** The SLO table is the enforced contract.
  The figures in `benchmarks/results/` and in the badge came from the reference
  machines recorded inside each JSON artifact (Linux x86_64, Windows). Re-run
  `selfcheck` or `smoke` to reproduce your own.
- **Peak RSS includes the interpreter.** The 26-28 MiB figures contain roughly
  25 MiB of CPython itself; the bounded-buffer pipeline adds about 2 MiB and does
  not grow with payload size (1 -> 10 GiB spread is ~0.1 MiB).
- **Environment-dependent defect found and fixed (1.1.1).** The suite asserted
  `codec == "deflate"` unconditionally while the streaming packer prefers
  `zstandard` whenever that optional package is installed: 50/51 with zstandard,
  51/51 without, and CI never installed zstandard. The deflate case now patches
  zstandard out, a zstd case covers the same bounded-chunk guarantee, and CI runs
  a `zstandard` without/with matrix.
- **Suite inventory guard.** `unittest discover` reports OK for whatever it can
  import, so a test module that disappears shrinks the suite silently.
  `tests/check_suite.py` fails the build unless every `tests/test_*.py` file
  contributed tests and the total stays at or above the expected floor.
- **Semantic search is optional.** The native ViBo semantic backend is not
  shipped here; semantic requests return lexical results with an explicit reason.
  The portable reference backend is lexical only.
- **Cloud providers are placeholders.** Yandex Disk and Google Drive adapters
  fail closed until token/OAuth handling and provider-specific range/sidecar
  reads land. No cloud credentials are required or enabled anywhere in CI.
- **Compression comparison caveat.** Deflate-based tools (`gzip`, `zip`,
  `tar.gz`) use a 32 KiB window, so payloads whose repeats exceed that window
  compress far worse than `zstd` or `7z`. The memory comparison is about RSS
  behaviour, not compression ratio.
- **10 GiB is not a pull-request gate.** The large profile runs only through the
  manual workflow or an explicit local command, and needs tens of GiB of scratch
  disk.
