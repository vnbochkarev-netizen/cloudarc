#!/usr/bin/env python3
"""CI guard: no test file may silently disappear from the suite.

``unittest discover`` reports "OK" for whatever it managed to import, so a
deleted or unimportable test module shrinks the suite without anyone noticing.
This guard runs discovery, then fails unless

* every ``tests/test_*.py`` file contributed at least one test,
* no module collapsed into an import-error placeholder, and
* the number of tests is at least the expected floor.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

EXPECTED_MIN_TESTS = 150


def main() -> int:
    tests_dir = pathlib.Path("tests")
    files = sorted(p.name for p in tests_dir.glob("test_*.py"))
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
        capture_output=True,
        text=True,
    )
    output = completed.stdout + completed.stderr
    problems: list[str] = []

    if completed.returncode != 0:
        problems.append(f"discovery exited with {completed.returncode}")

    match = re.search(r"^Ran (\d+) tests?", output, re.M)
    total = int(match.group(1)) if match else 0
    if not match:
        problems.append("could not read the test count from discovery output")
    elif total < EXPECTED_MIN_TESTS:
        problems.append(f"only {total} tests ran, expected at least {EXPECTED_MIN_TESTS}")

    imported = set(re.findall(r"\((?:tests\.)?(test_[a-z_]+)\.", output))
    missing = [name for name in files if name[:-3] not in imported]
    if missing:
        problems.append("these test files produced no tests: " + ", ".join(missing))

    if "_FailedTest" in output or "ERROR: test" in output:
        problems.append("a test module failed to import")

    print(f"test inventory: {len(files)} files, {total} tests, imported modules: {len(imported)}")
    for name in files:
        mark = "ok " if name[:-3] in imported else "MISSING"
        print(f"  {mark} {name}")
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        print(output[-4000:], file=sys.stderr)
        return 1
    print("test inventory OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
