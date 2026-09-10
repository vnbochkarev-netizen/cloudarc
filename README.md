# Vibo CloudArc MVP

![CloudArc SLO](docs/cloudarc-slo-badge.svg)


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
memory; payload bytes are not.

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
