from __future__ import annotations

import unittest

from core.native import probe_native


class RuntimeProbeTests(unittest.TestCase):
    def test_probe_is_honest(self):
        result = probe_native()
        self.assertIn("available", result)
        self.assertIn("reason", result)
