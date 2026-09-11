# CloudArc 1.3.1 - release handoff

**Date:** 2026-09-11  **Author:** Viacheslav Bochkarev  **Scope:** honesty about archive growth (found while testing the new wrapper skill)

## What was wrong

Building the `cloudarc-pack` wrapper skill surfaced a surprise on live data: a
17.6 MB folder of mixed documents produced an archive **larger** than the input.

```
raw 17593525 -> packed 18925073  saved_pct -7.57   index_length 4991978
raw 17593525 -> packed 13933806  saved_pct +20.8   index_length 554   (--no-index)
```

The lexical search index is stored **inside** the archive, and on this folder it
was 5.0 MB - more than the codec saved. The packer did not mention it: the result
simply said the archive was bigger.

## What 1.3.1 changes

- `pack` appends a warning when the archive grows:
  `archive is 7.6% LARGER than the input (18925073 vs 17593525 bytes): the manifest
  and search index cost more than the codec saved. Retry with --no-index, or
  accept a bigger but checksum-verified archive.`
  The CLI prints it on stderr; `result["warnings"]` carries it for callers.
- `tests/test_saving_warnings.py` (2 tests): an incompressible payload must warn,
  and the same payload with/without the index must show the index as the cause.

## Verification

- `python3 -m unittest discover -s tests` -> **85 tests, OK** (1 skipped); 1.3.0 had 83.
- `python3 tests/check_suite.py` -> inventory OK.
- Live check on the document folder: warning printed, and with `--no-index` the
  archive is 20.8% smaller than the input.

## Open question (for the owner)

The index costs ~5 MB per 17.6 MB of documents and is duplicated (it is also
written as a sidecar for remote search). Options:

1. keep the default as-is and rely on the new warning;
2. build the index only when asked (`--index`), keeping `--no-index` behaviour as
   the default for large trees - smaller archives and less RAM, no in-archive
   search;
3. shrink the index itself (top-N terms, bounded excerpts, deduplicated
   vocabulary) - a larger change, needs its own measurements.

Not decided here; nothing was published for this version.
