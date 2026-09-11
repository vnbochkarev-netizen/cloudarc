"""Safety policies for local and cloud-facing operations."""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .errors import SafetyError


PROTECTED_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv"}
PROTECTED_ROOT_NAMES = {"etc", "usr", "var", "bin", "sbin", "boot", "proc", "sys"}
WEEK_RE = re.compile(r"^\d{1,2}$")


def _parts_lower(path: Path) -> set[str]:
    return {part.lower() for part in path.parts}


def _leading_directory(resolved: Path) -> str | None:
    """Return the first directory component below the filesystem root.

    ``/root/app/bin/cli.js`` -> ``root``; ``/etc/passwd`` -> ``etc``.
    Windows drive anchors (``C:\\``) are not directory components and yield ``None``.
    """

    parts = [
        part
        for part in resolved.parts
        if part not in {"", "/", "\\", resolved.anchor}
    ]
    if not parts:
        return None
    head = parts[0].lower()
    if len(head) == 2 and head.endswith(":"):
        return None
    return head


def ensure_safe_input(path: Path, *, allow_system: bool = False) -> Path:
    """Reject protected directories and return a resolved path.

    A path is treated as a *system* path only when one of the protected roots is
    its leading directory (``/etc/passwd``, ``/usr/bin/python3``). A user tree
    that merely *contains* such a directory deeper down
    (``~/projects/app/bin/cli.js``) is allowed: rejecting it silently dropped
    legitimate project files from archives.

    ``allow_system=True`` lifts only the *system root* refusal, so that a log or
    data directory such as ``/var/log`` can be backed up on purpose. It never
    lifts the ``PROTECTED_NAMES`` refusal (``.git``, ``node_modules``, caches):
    those are re-creatable by design and stay out of archives.
    """

    resolved = path.expanduser().resolve(strict=False)
    if _parts_lower(resolved) & PROTECTED_NAMES:
        raise SafetyError(f"protected path is not allowed: {path}")
    if not allow_system and _leading_directory(resolved) in PROTECTED_ROOT_NAMES:
        raise SafetyError(f"system path is not allowed: {path}")
    return resolved


def protected_name_reason(path: Path) -> str | None:
    """Lexical protected-name check that never follows a symlink.

    Used for entries whose link must be preserved rather than resolved: calling
    :func:`ensure_safe_input` on them would follow the link and (for a link into
    ``/etc``) report a "system path" for a file that only *points* there.
    """

    if _parts_lower(path) & PROTECTED_NAMES:
        return "protected"
    return None


def ensure_within(root: Path, candidate: Path) -> Path:
    """Resolve a candidate and ensure it stays below root."""

    root_resolved = root.expanduser().resolve(strict=False)
    candidate_resolved = candidate.expanduser().resolve(strict=False)
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise SafetyError(f"path escapes allowed root: {candidate}") from exc
    return candidate_resolved


def validate_week(value: str) -> str:
    if not WEEK_RE.fullmatch(value):
        raise SafetyError("week must be a numeric value from 0 to 99")
    return f"{int(value):02d}"


def validate_project(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned in {".", ".."}:
        raise SafetyError("project name must be non-empty")
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise SafetyError("project name must not contain path separators or '..'")
    return cleaned


def weekly_folder(
    *,
    year: str,
    smartbot_root: str,
    week: str,
    project: str,
) -> str:
    week_value = validate_week(week)
    smartbot_value = validate_project(smartbot_root)
    project_value = validate_project(project)
    if not str(year).isdigit() or len(str(year)) != 4:
        raise SafetyError("year must be a four-digit value")
    return f"{year}/{smartbot_value}/неделя-{week_value}/{project_value}"


def backup_existing(path: Path) -> Path | None:
    """Create a sibling backup before an overwrite."""

    if not path.exists():
        return None
    if path.is_symlink():
        raise SafetyError(f"refusing to back up a symlink: {path}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{index}")
        index += 1

    if path.is_dir():
        # A tree can legitimately contain dangling symlinks (CloudArc 1.4.0
        # stores them on purpose). copytree without ``symlinks=True`` follows
        # them and dies with "[Errno 2] No such file or directory", leaving a
        # half-written backup behind.
        shutil.copytree(path, candidate, symlinks=True, ignore_dangling_symlinks=True)
    else:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, candidate)
    return candidate


def require_apply_for_existing(path: Path, apply: bool) -> None:
    if path.exists() and not apply:
        raise SafetyError(
            f"refusing to overwrite existing path {path}; rerun with --apply"
        )


def require_apply_for_mutation(apply: bool, operation: str) -> None:
    if not apply:
        raise SafetyError(
            f"{operation} is a mutating operation; preview with --dry-run or confirm with --apply"
        )
