# CloudArc 1.2.3 - release handoff

**Date:** 2026-09-11  **Author:** Viacheslav Bochkarev  **Scope:** patch, backup reliability

## What was wrong

Found by pointing the nightly backup at the real agent data set (2.2 GB:
`/root/vibo` + `/root/.hermes`):

```
{"error": "FileNotFoundError",
 "message": "[Errno 2] No such file or directory: '/root/.hermes/kanban.db-shm'"}
```

SQLite removes its `-shm`/`-wal` siblings when the last connection closes, logs
rotate, editors swap files: a file that exists during the directory walk can be
gone microseconds later. The whole archive died on it - the same failure class as
the symlink defect fixed in 1.2.1, and fatal for any backup of a live system.

## What 1.2.3 changes

- `pack` and `analyze` report a file that disappears between the walk and the
  read as `skipped` with reason **`vanished`** instead of raising
  `FileNotFoundError`. `pack` also re-checks existence before spooling.
- Skipped reasons now cover: `protected`, `system`, `symlink`, `vanished`.
- `tests/test_safety_and_skips.py` gains `VanishedFileTests` (2 tests) covering
  both `pack` and `analyze`.

## Verification

- `python3 -m unittest discover -s tests` -> **78 tests, OK** (1 skipped); 1.2.2 had 76.
- Real run (the case that failed): `pack /root/vibo /root/.hermes --no-index
  --dedup` -> 6319 entries, 55155 skipped (`protected` 55081, `symlink` 74),
  0.97 GB archive, **89 MiB peak RSS**, 26 s, no failure.
- `python3 tests/check_suite.py` -> inventory OK.

Docs are unchanged in this release.
