# CloudArc 1.4.0 - release handoff

**Date:** 2026-09-11  **Author:** Viacheslav Bochkarev  **Scope:** a restore must rebuild the tree, not just the bytes

## What was wrong

A pack of a real tree returned file *contents* only:

```
$ cloudarc.py pack /tmp/t1 -o t1.vibo --apply     # 1.3.1
... "skipped_by_reason": {"symlink": 2}
raw 24 -> packed 10656 bytes
```

* permission bits were never stored, so `640` came back as `644` and `750` as `644`;
* symlinks found in a tree were skipped and reported (the target was lost);
* empty directories did not exist as entries, so they vanished on unpack.

Measured on the live trees used for this work: `/root/vibo/docs` packs 249
entries, `/root/.hermes/scripts` 49 - with 1.3.1 the same trees came back with
every mode reset and no link kept. A backup that cannot rebuild the machine it
came from is not a backup.

## What 1.4.0 changes

- **Metadata per entry.** `kind` (`file` / `symlink` / `dir`), `mode`,
  `mtime_ns`; symlink entries add `target`. All fields are optional on read, so
  archives written by 1.3.x restore unchanged (a missing `kind` means `file`).
- **Directories are entries.** Zero-length `dir` payloads, so empty directories
  survive and directory permissions are applied *after* their children exist.
- **Symlinks are stored, never followed.** The payload is the target string
  (a 5 MB file behind a link adds ~40 bytes to the archive); the link is
  recreated on unpack. Absolute, relative and broken links all round-trip.
  An explicitly passed symlink is still refused (fail-closed).
- **Two-pass unpack.** Regular files and directories first, links afterwards,
  then directory permissions. No entry can be written *through* a link, so the
  classic tar/zip symlink escape cannot leave the output directory.
- **Validation refuses the escape outright.** `validate_manifest` raises
  `FormatError: archive writes through a symlink` for an archive that declares a
  symlink and anything below it; `_restore_symlink` raises `SafetyError` on a
  name collision and re-checks containment before creating a link.
- **`--allow-system`** lifts *only* the leading-system-root refusal
  (`/etc`, `/var`, `/usr`, `/bin`, `/sbin`, `/boot`, `/proc`, `/sys`) for `pack`,
  `analyze` and `unpack` - a deliberate `/var/log` backup or a restore below
  `/var` is now possible. Protected names (`.git`, `node_modules`, `__pycache__`,
  `venv`) stay refused in every mode.
- **`--no-metadata`** keeps the 1.3.x contents-only behaviour on both sides.
- **Forward compatibility is now fail-closed.** A 1.x reader ignores unknown
  manifest fields, so before this change it would have "restored" a directory as
  an empty file and a symlink as a text file containing the target - a silent
  wrong restore. An archive whose entries include directories or links now
  declares container `format_version = 2`; a 1.3.x reader refuses it with
  `unsupported VIBO format version`. Archives of plain files stay version 1, so
  contents-only interop is preserved.
- **Protected subtrees are reported once**, as the directory, instead of listing
  every child (`project/.git -> protected` rather than hundreds of lines).
- `scripts/verify_restore.py`: compares kind, content hash, mode and mtime of
  every entry between the archive, the original tree and the restored tree.
- Only `kind == "file"` entries enter the lexical index (directory markers and
  link targets are metadata, not searchable content).

## Cloud: Yandex Disk, and a live-log defect

Two things surfaced while preparing the Kraba test:

1. **Both cloud adapters were placeholders** (`CloudNotConfigured`), so no cloud
   path could be tested anywhere. `cloud/yandex.py` is now a real provider for the
   Yandex Disk REST API v1: token from the environment or a file (never printed),
   `list_folder`/`get_meta`/`ensure_folder`/`upload`/`download`/`delete`/`find`,
   two-step streamed uploads with `Content-Length`, size-verified downloads, and
   **HTTP Range reads** so `open`/`fetch` search a remote archive without
   downloading it. Google Drive stays a fail-closed placeholder.
   `tests/test_yandex_cloud.py` (24 tests) replaces both network seams, so the
   adapter is fully exercised offline.
2. **One growing log file aborted an entire backup.** Packing `/var/log` on the
   live server failed with `source changed while packing: /var/log/syslog` - a
   single actively written file killed a 5.8 GB archive. Such files are now
   skipped and reported (`reason: changed`), and `pack --accept-changing` stores a
   torn snapshot with `changing: true` in the manifest plus an explicit warning.
   Verified live: `/var/log` (5.8 GB, 234 files) packs in **21.8 s at 32 MB peak
   RSS**, and the three biggest entries restore from the archive with matching
   hashes.

## Defects found and fixed while testing 1.4.0

1. `os.chmod(..., follow_symlinks=False)` is unavailable on Linux, so restoring a
   link logged a warning per link. Modes are no longer applied to symlinks
   (Linux has no `lchmod`); `mtime` still is.
2. `--allow-system` did not reach `_validate_pack_output` and `unpack`, so a
   system path was refused even with the flag. Both now thread the flag through.
3. A 1.3.x engine "restored" a 1.4.0 archive into a wrong tree instead of
   refusing it (directories became empty files, links became text files). The
   container format version now marks metadata archives as version 2, which old
   readers reject (see "Forward compatibility" above).
4. The review round added five more fixes - see the table below.

## Review round: what two independent reviewers found

The change was reviewed before any release by two subagents (a red team hunting
data-loss/escape defects with reproductions, and a diff reviewer running the
suite, the memory benchmark and cross-version checks). All seven findings are
fixed and each fix has a regression test in `tests/test_metadata_restore.py`
(class `RootAndRegressionTests`).

| # | Sev | Defect (reproduced by the reviewer) | Fix |
| --- | --- | --- | --- |
| 1 | blocker | A tree whose top level held nothing but symlinks crashed `unpack` (`FileNotFoundError`) and restored nothing: no entry ever created the destination directory | the input root is an entry (see above); links also create a missing parent as a second line of defence |
| 2 | major | The root directory's mode/mtime were lost silently - `0700` came back as `0755`, weakening a private backup | same fix: the root entry carries `mode`/`mtime_ns` |
| 3 | major | `unpack --apply` over a directory containing a dangling symlink died in `backup_existing` (`shutil.copytree` followed the link) and left a half-written `*.bak-*` behind | `copytree(..., symlinks=True, ignore_dangling_symlinks=True)` |
| 4 | major | `pack --dry-run` leaked `.cloudarc-pack-*` (with its metadata payloads) into the output directory on every run | staging is removed before the dry-run plan is returned |
| 5 | major | A 1.x reader met a 1.4.0 archive with `FileExistsError` mid-restore instead of refusing it | container `format_version = 2` for metadata archives (verified against the real 1.3.1 engine: `unsupported VIBO format version`) |
| 6 | minor | `mtime_ns = 10**40` (valid per the manifest, reachable from a hostile archive) raised an unhandled `OverflowError` out of `os.utime` | becomes a `metadata_warnings` entry; the CLI also handles `OverflowError`/`OSError` |
| 7 | minor | `push`/`pull` did not accept or forward the new flags, and `pull` never surfaced metadata warnings | both parsers and both call sites thread `--no-metadata`/`--allow-system`, and `pull` calls `_warn_metadata` |

Two more consequences of fix 1/2, both now covered by tests: a tree of nothing
but empty directories packs (it used to fail with "no files found to pack"), and
a directory input's archive is always version 2.

Honest correction from the review: peak RSS on 200 MiB of text is **31.9-32.1 MB**
(1.3.1: 31.5 MB), i.e. +0.4-0.5 MB (1.2-1.6%). "Memory does not grow with the
payload" still holds; "memory did not change at all" would have been too strong.

## Verification

```
$ python3 -m pytest tests -q
120 passed, 8 subtests passed in 23.89s        # 1.3.1: 85 passed
$ python3 tests/check_suite.py
test inventory: 12 files, 120 tests ... test inventory OK
```

Cross-version check with the real 1.3.1 engine from the cache, on a 1.4.0
archive that contains a subdirectory and a symlink:

```
$ python3 cloudarc.py unpack /tmp/fc.vibo -o /tmp/fc-out --apply    # engine 1.3.1
CloudArc error: unsupported VIBO format version
```

Live runs (Linux x86_64, Python 3.11.15, zstandard 0.25.0):

| check | result |
| --- | --- |
| synthetic tree (4 files, 3 links, 4 dirs incl. empty, unicode + spaces) | pack 11 entries -> unpack -> **0 mismatches** (mode, mtime, targets, bytes) |
| `/root/vibo/docs` (249 entries) | `verify_restore.py` -> `entries_checked 249, problems [], verdict OK` |
| `/root/.hermes/scripts` (49 files) | `verify_restore.py` -> 49 checked, 0 problems |
| 200 MiB text, `--no-index --workers 1` | peak RSS **31.7-32.1 MB**, 0.8-0.9 s (1.3.1: 31.5 MB) - flat, +1.2-1.6% |
| archive written with `--no-metadata` | restores with `restore_metadata=True`, 0 warnings |
| `pack /etc/hostname` | refused (`system path is not allowed`) -> with `--allow-system` packs and restores |

## Not done / honest limits

- No hardlinks, xattrs, ACLs or ownership (`uid`/`gid`) restore - modes and
  mtimes only. A restore onto another machine therefore keeps files readable and
  writable by the same *user*, not the same numeric ids.
- Special files (FIFOs, sockets) are skipped and reported as `special`.
- Windows was not re-tested for this version; symlink tests skip there by design.
- CI was not run and nothing was published: the engine is a local tree copy
  (`/root/cloudarc-src`), the cache still serves 1.3.0.

## Publishing steps (needs the owner's go-ahead)

1. hash the tree and commit; tag `v1.4.0` and push to
   `vnbochkarev-netizen/cloudarc`;
2. run `CloudArc CI` on the tag and check `headSha` matches;
3. build the release archive + `SHA256SUMS.txt` and attach it;
4. `cloudarc_pack.py --version 1.4.0 setup` on each host, then re-run
   `verify_restore.py` on live data before switching the default version.
