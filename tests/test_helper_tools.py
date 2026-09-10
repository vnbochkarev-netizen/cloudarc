"""Tests for the helper diagnostics added in 1.2.0 (doctor, badge, selfcheck)."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = (
    REPO_ROOT
    / "skills"
    / "cloudarc-bounded-memory-benchmark"
    / "scripts"
    / "cloudarc_benchmark.py"
)


def _load_helper():
    """Import the helper lazily.

    The helper is loaded inside ``setUpClass`` rather than at module import time
    so that a broken helper surfaces as a failing test in this file instead of
    turning the whole module into one opaque import error.
    """

    spec = importlib.util.spec_from_file_location("cloudarc_benchmark_helper", HELPER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper from {HELPER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _HelperTestCase(unittest.TestCase):
    helper = None

    @classmethod
    def setUpClass(cls):
        cls.helper = _load_helper()


def _payload(peak=28311552, limit=268435456, passed=True):
    return {
        "schema": "cloudarc.large-package-benchmark",
        "slo": {"peak_rss_bytes_max": limit},
        "evaluation": {"pass": passed},
        "runs": [{"pack": {"peak_rss_bytes": peak, "wall_seconds": 1.0},
                  "unpack": {"peak_rss_bytes": peak, "wall_seconds": 1.0}}],
    }


class BadgeTests(_HelperTestCase):
    def test_pass_badge_reports_peak_and_limit(self):
        svg = self.helper.render_slo_badge(_payload())
        self.assertTrue(svg.startswith("<svg"))
        self.assertTrue(svg.strip().endswith("</svg>"))
        self.assertIn("PASS", svg)
        self.assertIn("27.0 / 256 MiB", svg)
        self.assertIn("#2f7d32", svg)

    def test_fail_badge_uses_failure_colour(self):
        svg = self.helper.render_slo_badge(_payload(passed=False))
        self.assertIn("FAIL", svg)
        self.assertIn("#b3261e", svg)

    def test_badge_without_runs_is_explicit(self):
        svg = self.helper.render_slo_badge({"slo": {"peak_rss_bytes_max": 1024}, "runs": []})
        self.assertIn("no data", svg)
        self.assertIn("#5f6368", svg)

    def test_badge_output_is_written_or_streamed(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "result.json"
            target = Path(temp) / "badge.svg"
            source.write_text(json.dumps(_payload()), encoding="utf-8")
            rc = self.helper.main(["badge", "--input", str(source), "--output", str(target)])
            self.assertEqual(rc, 0)
            self.assertIn("PASS", target.read_text(encoding="utf-8"))


class DoctorTests(_HelperTestCase):
    def test_doctor_is_ready_on_the_repository(self):
        lines, ready = self.helper._doctor_lines(REPO_ROOT, "python")
        self.assertTrue(ready, "\n".join(lines))

    def test_doctor_is_not_ready_outside_the_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            lines, ready = self.helper._doctor_lines(Path(temp), "python")
            self.assertFalse(ready)
            self.assertIn("NOT READY", "\n".join(lines))

    def test_doctor_reports_zstandard_state(self):
        lines, _ = self.helper._doctor_lines(REPO_ROOT, "python")
        text = "\n".join(lines)
        self.assertIn("zstandard", text)
        self.assertIn("/proc VmRSS", text)


if __name__ == "__main__":
    unittest.main()
