# CloudArc Bounded-Memory Benchmark 1.2.3

**Release date:** 2026-09-11

## Highlights (1.2.3)

- Files that vanish mid-run (SQLite `-shm`/`-wal` siblings, rotating logs) are now
  reported as `skipped: vanished` instead of aborting the archive. Found by
  backing up a live 2.2 GB data set: the pack died on `/root/.hermes/kanban.db-shm`.
- Skipped reasons: `protected`, `system`, `symlink`, `vanished`.
- Product suite: 76 -> 78 tests.

## Highlights (1.2.2)

- Version drift fixed: `cloudarc.py version` reported `0.1.0-mvp` while
  `pyproject.toml` said 1.2.1. `core/version.py` is now the single source of
  truth and `tests/test_version.py` fails the build on any mismatch.
- Product suite: 71 -> 76 tests.

## Highlights (1.2.1)

- Dogfooding a 556-file heterogeneous tree exposed three defects, all fixed in the
  product: paths containing a `bin`/`var`/`etc` component were rejected anywhere
  (any Node or Python CLI project lost its `bin/` directory); skipped files were
  never reported (556 files in, 356 out, no signal); one symlink inside a
  directory aborted the whole walk.
- `pack --no-index` added: 169 MiB -> 29.7 MiB peak RSS on the same 64.1 MiB text
  tree, archive and restore unchanged, `search.index_built = false`.
- `pack`/`analyze` return `skipped`, `skipped_count`, `skipped_by_reason`; the CLI
  warns on stderr about files that did not enter the archive.
- README and `docs/LARGE_PACKAGE_SLO.md` now carry the measured heterogeneous-tree
  RSS numbers next to the streaming 256 MiB SLO.
- Product test suite: 59 -> 71 tests (`tests/test_safety_and_skips.py`),
  `tests/check_suite.py` inventory guard OK, helper smoke PASS.

## Highlights (1.2.0)

- Helper `doctor`, `selfcheck` and `badge` commands; `[zstd]`/`[semantic]` extras;
  lazy `zstandard` import.

# CloudArc Bounded-Memory Benchmark 1.1.0

**Release date:** 2026-09-10

## Highlights

- Added `scripts/cloudarc_benchmark.py` with three safe entry points:
  `smoke`, `manual`, and `check-telemetry`.
- Added explicit `--yes` protection for the manual 1/5/10 GiB profile.
- Added strict checks for positive RSS and non-zero bytes when telemetry
  reports successful range or sidecar reads.
- Added post-run validation of JSON schema/status, multi-file cardinality, and
  matching Markdown `PASS` reports before the helper exits successfully.
- Updated the pull request CI workflow to invoke the helper and upload JSON and
  Markdown smoke artifacts.
- Kept the large manual workflow separate from normal CI and preserved the
  single-file and multi-file SLO boundaries.

## Verification

- Skill validation: passed.
- CloudArc test suite: 51/51 passed.
- Helper smoke profile: passed on the reference Windows environment.
- Installed profile copy verified with the helper's range and sidecar telemetry
  checks.

## Upgrade note

Replace older copies of `cloudarc-bounded-memory-benchmark` with this package.
The skill does not enable cloud credentials or activate the native ViBo
backend.
