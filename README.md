# CloudArc

**Pack a 10 GiB folder into a single `.vibo` archive at a flat ~28 MiB peak RSS — then read its
metadata remotely without ever fetching the payload.**

Apache-2.0 · no cloud credentials needed to try · Python 3.11+

![CloudArc SLO](docs/cloudarc-slo-badge.svg)
![Peak RSS vs payload size](docs/cloudarc-rss-vs-size.png)

| payload | pack peak RSS | unpack peak RSS |
|---|---|---|
| 1 GiB | 27.7 MiB | 26.9 MiB |
| 5 GiB | 27.8 MiB | 26.8 MiB |
| 10 GiB | **27.8 MiB** | **26.9 MiB** |

Flat: ~25 MiB of that is CPython itself, the pipeline adds ~2 MiB and does **not** grow with
payload size (the normative SLO ceiling is 256 MiB — an order of magnitude of headroom).

### What it does that `tar` and `zip` do not

1. **Metadata reads never touch the data section.** `info`, `list` and `search` work through HTTP
   range requests; CI rejects any response that claims `data_section_read` — the payload is never
   fetched to answer a metadata question.
2. **High-cardinality multi-file packages.** A 5,000-file profile with a versioned manifest and an
   index document per entry, plus content dedup by SHA-256.
3. **Safety defaults.** Everything is a preview unless `--apply`/`--yes`; overwriting creates a
   sibling backup; unpack verifies size and SHA-256 per entry; path escapes and protected
   directories are rejected.

### Try it in three commands

```bash
git clone https://github.com/vnbochkarev-netizen/cloudarc && cd cloudarc
python cloudarc.py pack ./documents -o ./out/documents.vibo --apply
python cloudarc.py search ./out/documents.vibo "quarterly report" --mode semantic
```

---

## Runtime

- Python 3.11 or newer for the reference backend.
- The native ViBo extension must be built for the interpreter that runs CloudArc
  (`cpython-311`, `cpython-312`, …); CloudArc probes by import and reports which
  builds are present when the current one is missing. Use
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

## Speed, codecs and parallelism (1.3.0, measured)

| what | result |
|---|---|
| text, 64 MiB profile: archive | 114 973 KiB (deflate) -> **18 461 KiB (zstd) = 6.2x smaller** |
| text, 64 MiB profile: pack | 22.2 -> 26.6 MiB/s (+20%); unpack 0.33 -> 0.12 s |
| real 2.2 GB mixed tree (`--no-index --dedup`) | 1.44 GB archive, 14.76% saved, 27.6 s, 87 MiB peak RSS |
| 4 x 32 MiB payloads, `--workers 4` | 18.22 s -> 5.13 s (**3.55x**), peak tree RSS 49 -> 155 MiB |
| 4000 x 16 KiB, `--workers 4` | 10.28 s -> 4.50 s (**2.28x**), peak tree RSS 45 -> 117 MiB |
| 50 tiny files, `--workers 4` | 0.063 s -> 0.083 s (**0.76x - the pool loses**) |

`zstandard` is an optional accelerator (the CLI core keeps zero required
dependencies; packaged binaries bake it in). When it is missing, the fallback to
deflate is reported instead of silent:

```
warning: zstandard is not installed: text payloads fall back to deflate (slower and larger)...
```

`pack --workers N` spreads file work over processes. It is **not** the default:
above 16 MiB total and with at least two files the pool is chosen automatically
(2.3-3.7x measured), below that the sequential path is faster. The pool raises
peak tree RSS to at most ~155 MiB (inside the SLO) and always warns when used;
any pool failure falls back to sequential packing rather than failing.

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

### Yandex Disk (1.4.0)

The Yandex Disk adapter talks to the REST API v1 and needs an OAuth token with
`cloud_api:disk.read`/`cloud_api:disk.write`. A token issued with only
`cloud_api:disk.app_folder` also works - set `YANDEX_DISK_ROOT=app:/` and every
request stays inside the application folder. The token comes from the
environment or from a file:

```bash
export YANDEX_DISK_TOKEN=...          # or YANDEX_DISK_TOKEN_FILE=/path/to/token
cloudarc.py push ./tree --disk yd --week 36 --project Weekly --apply
cloudarc.py ls-cloud --disk yd --week 36 --project Weekly
cloudarc.py open  2026/ROOT/неделя-36/Weekly/tree.vibo info
cloudarc.py open  2026/ROOT/неделя-36/Weekly/tree.vibo search "connection refused"
cloudarc.py pull  2026/ROOT/неделя-36/Weekly/tree.vibo --week 36 --project Weekly -o ./restored --apply
```

`open`/`fetch` read the archive header, manifest and index with HTTP `Range`
requests, so a remote search downloads a few kilobytes instead of the archive.
Uploads are two-step (a ticket URL, then a streamed `PUT` with `Content-Length`),
downloads are size-verified, and every mutation still requires `--apply`. The
token is read from the environment or a file, never printed, and a missing token
fails closed with a message naming the environment variable to set.

Google Drive remains an explicit placeholder that fails closed until OAuth
credentials and provider-specific reads are implemented.

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

## Restoring a server, not just file contents (1.4.0)

Up to 1.3.1 a restore returned file *contents*: permission bits were dropped
(`640` came back as `644`), symlinks were skipped, and empty directories
disappeared. Packing a real tree showed it plainly - 375 of 3919 files survived
and every mode came back as `644`. A backup you cannot rebuild a machine from is
not a backup, so 1.4.0 records and restores metadata:

- every entry carries `kind` (`file` / `symlink` / `dir`), `mode`, `mtime_ns`;
  symlink entries also carry `target` (the link target string is the payload, and
  the link is never followed while packing);
- empty directories survive as `dir` entries, and directory permissions are
  applied after their children exist;
- `unpack` is two-pass: files and directories first, links afterwards, so no
  entry can be written *through* a link and escape the output tree. An archive
  that declares a symlink and then stores anything below it is refused at
  validation time (`archive writes through a symlink`);
- `pack --no-metadata` / `unpack --no-metadata` keep the old contents-only
  behaviour, and archives written by 1.3.x still restore unchanged;
- the input directory itself is an entry, so its own mode and timestamp are
  restored too (a private `0700` tree no longer comes back as `0755`), a tree made
  only of symlinks or empty directories round-trips, and an over-the-top restore
  that meets a dangling symlink no longer dies in the pre-overwrite backup;
- an archive with directory or link entries - that is, any directory input -
  declares **format version 2**, so a 1.3.x reader refuses it
  (`unsupported VIBO format version`) instead of restoring a directory as an
  empty file and a symlink as a small text file. Packing single files stays
  version 1 and older readers keep reading those;
- `--allow-system` lifts *only* the system-root refusal, so `/var/log` can be
  backed up on purpose; `.git`, `node_modules` and caches stay out in any case.

Verify a restore instead of trusting it:

```bash
python3 cloudarc.py pack /srv/data -o data.vibo --apply --json
python3 cloudarc.py unpack data.vibo -o /srv/restore --apply --json
CLOUDARC_SRC=. python3 scripts/verify_restore.py data.vibo /srv/data /srv/restore
# -> {"entries_checked": 249, "problems": [], "verdict": "OK"}
```

The checker compares kind, content hash, permission bits and modification time
for every manifest entry against the original tree.

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
  (`.git`, `__pycache__`, `.venv`, `node_modules`), leading system paths (unless
  `--allow-system`) and special files (FIFOs, sockets) are not packed. `pack`/`analyze` now return
  `skipped`, `skipped_count` and `skipped_by_reason`, and the CLI prints a
  `warning: N file(s) were not packed (...)` line on stderr (suppressed by
  `--json`). Before 1.2.1 that information did not exist: a 556-file tree
  produced a 356-file archive with no signal at all.
- **`bin/`, `var/`, `etc/` are names, not system paths (1.2.1).** Only a
  *leading* system directory (`/etc/...`, `/usr/...`, `/var/...`, `/proc/...`)
  is refused. A project tree containing `bin/cli.js`, `var/cache.txt` or
  `app/etc/config.yml` is packable; those files used to be dropped silently,
  which cost any Node or Python CLI project its `bin/` directory.
- **One symlink no longer aborts a whole directory (1.2.1).** A symlink passed
  explicitly on the command line is refused (fail-closed), and symlinks are never
  dereferenced. Since 1.4.0 a symlink found while walking a directory is *stored*
  as a link entry and recreated on unpack (see "Restoring a server" below);
  before 1.4.0 it was skipped and reported with reason `symlink`.

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
- **Cloud provider status.** Yandex Disk (REST v1, OAuth token) is implemented
  and unit-tested offline; its live behaviour against a real account has to be
  verified per deployment. Google Drive stays a fail-closed placeholder. No cloud
  credentials are required or enabled anywhere in CI: the provider tests replace
  both network seams.
- **Compression comparison caveat.** Deflate-based tools (`gzip`, `zip`,
  `tar.gz`) use a 32 KiB window, so payloads whose repeats exceed that window
  compress far worse than `zstd` or `7z`. The memory comparison is about RSS
  behaviour, not compression ratio.
- **10 GiB is not a pull-request gate.** The large profile runs only through the
  manual workflow or an explicit local command, and needs tens of GiB of scratch
  disk.
