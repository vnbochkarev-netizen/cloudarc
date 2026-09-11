# CloudArc 1.2.1 - release handoff

**Date:** 2026-09-11  **Author:** Viacheslav Bochkarev  **Scope:** patch, no format change
**Triggered by:** the first real dogfooding run on a 556-file heterogeneous tree.

## What was wrong (all three found by using the tool, not by reading it)

1. **Path policy rejected user files whose path merely *contained* a
   system-looking name.** `core/safety.py` refused any path with a component in
   `{etc, usr, var, bin, sbin, boot, proc, sys}`. `~/projects/app/bin/cli.js` was
   therefore unpackable, and directory walks dropped such files silently. Any
   Node/Python CLI project lost its `bin/` directory.
2. **Skipped files vanished without a word.** A 556-file tree produced a
   356-file archive; 198 protected files (`.git`, `__pycache__`) and 2 files
   dropped by defect #1 were never reported. For a backup tool this is the worst
   possible failure mode.
3. **A single symlink aborted the whole directory walk** - one symlink in a tree
   made `analyze` fail with `symlink input is not allowed`.
4. **Reported memory numbers only held for streaming payloads.** The lexical
   index is built in memory, so a 64.1 MiB text tree peaked at 169 MiB while the
   README advertised 26-28 MiB. The claim was narrow; the tool had no escape
   hatch.

## What 1.2.1 changes

- `ensure_safe_input` now refuses a path only when a protected root is its
  **leading** directory (`/etc/passwd`, `/usr/bin/env`). Deeper `bin/var/etc`
  components are ordinary directories. Windows drive anchors are unaffected.
- `_discover_files` returns `(files, skipped)`; `pack` and `analyze` report
  `skipped`, `skipped_count`, `skipped_by_reason`; the CLI prints
  `warning: N file(s) were not packed (...)` on stderr (`--json` carries the full
  list instead).
- Symlinks found while walking a directory are skipped with reason `symlink`
  instead of aborting the directory. An explicitly passed symlink is still
  refused (fail-closed) and symlinks are never dereferenced.
- New `pack --no-index` (also on `push`): skips index construction entirely.
  The archive, payload pipeline and restore are unchanged; `search` returns
  nothing and the manifest records `search.index_built = false`. On the 64.1 MiB
  dogfooding tree this is 169 MiB -> 29.7 MiB peak RSS and ~9 points smaller
  sidecars. `default_mode` stays `lexical`, so the format contract is untouched.
- `pack`'s result keeps `index` as the sidecar *path* (compatibility) and gains
  `index_built: bool`.
- `TOOL_VERSION` was stuck at `0.1.0-mvp` inside 1.2.0 manifests; it is now
  `1.2.1`.
- README and `docs/LARGE_PACKAGE_SLO.md` document the measured heterogeneous-tree
  numbers instead of implying the streaming limit is universal.

## Verification

- `python3 -m unittest discover -s tests` -> **71 tests, OK (1 skipped)** (was 59;
  `tests/test_safety_and_skips.py` adds 12 covering all four defects).
- `python3 tests/check_suite.py` -> `test inventory: 9 files, 71 tests` -> OK.
- helper `smoke` (CI profile) -> both profiles `pass: true`; `doctor` -> READY,
  package 1.2.1.
- Dogfooding re-run: `bin/context-compactor.js` is now packed, 198 protected
  files reported by reason, `--no-index` peaks at 29.7 MiB.

## Not in this release

Semantic search, cloud providers and the native ViBo runtime are unchanged.
Compression behavior is unchanged.
