from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "mcon" / "src"))

from mcon.adapters.codex import _codex_environment


class CodexAdapterTests(unittest.TestCase):
    def test_codex_environment_sets_defaults_without_overwriting(self) -> None:
        environment = _codex_environment([("CODEX_HOME", "/custom/codex"), ("OTHER", "value")])

        self.assertEqual(environment["CODEX_HOME"], "/custom/codex")
        self.assertEqual(environment["MCON_WORKSPACE"], "/workspace")
        self.assertEqual(environment["OTHER"], "value")


if __name__ == "__main__":
    unittest.main()
