#!/usr/bin/env python3
"""CI helper: VERSION file and SKILL.md frontmatter must agree."""
import pathlib
import re
import sys


def main() -> int:
    version = pathlib.Path("VERSION").read_text(encoding="utf-8").strip()
    skill = pathlib.Path("SKILL.md").read_text(encoding="utf-8")
    match = re.search(r'^version:\s*"?([^"\n]+)"?', skill, re.M)
    frontmatter = match.group(1).strip() if match else None
    print(f"VERSION={version!r} frontmatter={frontmatter!r}")
    if frontmatter != version:
        print("ERROR: VERSION and SKILL.md frontmatter disagree", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
