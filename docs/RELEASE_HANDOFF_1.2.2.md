# CloudArc 1.2.2 - release handoff

**Date:** 2026-09-11  **Author:** Viacheslav Bochkarev  **Scope:** patch, version consistency only

## What was wrong

The version number lived in three places and two of them were stale:

- `cloudarc.py` had `VERSION = "0.1.0-mvp"`, so `cloudarc.py version` reported
  `cloudarc_version: 0.1.0-mvp` on the 1.2.1 release.
- `core/packer.py` had `TOOL_VERSION = "0.1.0-mvp"` until 1.2.1 (fixed there);
  the manifests of 1.2.0 carry the stale string.
- `pyproject.toml` and `skills/.../VERSION` moved on their own.

Found by running `cloudarc.py version` on the installed 1.2.1 package during
dogfooding, right after shipping it.

## What 1.2.2 changes

- New `core/version.py` is the single source of truth: it reads `version` from
  `pyproject.toml` (with a constant fallback when the file is absent).
  `cloudarc.py` and `core/packer.py` import it (`TOOL_VERSION = VERSION`).
- New `tests/test_version.py` (5 tests) fails the build on any drift: the stale
  `mvp` string, a `pyproject.toml` mismatch, a CLI/manifest mismatch, and the
  packaged skill `VERSION`.
- Docs example strings updated (`docs/ARCHITECTURE.md`, `docs/VIBO_CONTRACT.md`).

## Verification

- `python3 -m unittest discover -s tests` -> 76 tests, OK (1 skipped); was 71.
- `python3 tests/check_suite.py` -> inventory OK.
- `python3 cloudarc.py version` -> `cloudarc_version: 1.2.2`;
  `pack` writes `tool_version: 1.2.2` into the manifest.

## Not in this release

No behavior change beyond version reporting. Archive format, path policy, skip
reporting and `--no-index` are exactly as in 1.2.1.
