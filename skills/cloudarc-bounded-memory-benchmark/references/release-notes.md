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
