"""Single source of truth for the CloudArc version.

The CLI reported ``0.1.0-mvp`` while ``pyproject.toml`` said ``1.2.1`` (and the
manifest ``tool_version`` had the same stale string in 1.2.0). Three copies of
one number is two too many: everything now reads from here, and a test asserts
that ``pyproject.toml`` agrees.
"""

from __future__ import annotations

import re
from pathlib import Path

FALLBACK_VERSION = "1.4.0"
PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"


def package_version() -> str:
    """Return the version declared in ``pyproject.toml`` (fallback: constant)."""

    try:
        text = PYPROJECT_PATH.read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_VERSION
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    return match.group(1) if match else FALLBACK_VERSION


VERSION = package_version()
