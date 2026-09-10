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


def ensure_safe_input(path: Path) -> Path:
    """Reject protected directories and return a resolved path."""

    resolved = path.expanduser().resolve(strict=False)
    parts = _parts_lower(resolved)
    if parts & PROTECTED_NAMES:
        raise SafetyError(f"protected path is not allowed: {path}")
    if any(part in PROTECTED_ROOT_NAMES for part in parts):
        # Do not reject a Windows drive name; only reject actual Unix-style
        # protected directory components.
        if any(part in {"etc", "usr", "var", "bin", "sbin", "boot", "proc", "sys"} for part in parts):
            raise SafetyError(f"system path is not allowed: {path}")
    return resolved


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
        shutil.copytree(path, candidate)
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
