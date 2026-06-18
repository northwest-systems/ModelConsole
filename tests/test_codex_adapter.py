from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "mcon" / "src"))

from mcon.adapters import codex
from mcon.adapters.codex import _codex_environment


class CodexAdapterTests(unittest.TestCase):
    def test_codex_environment_sets_defaults_without_overwriting(self) -> None:
        environment = _codex_environment([("CODEX_HOME", "/custom/codex"), ("OTHER", "value")])

        self.assertEqual(environment["CODEX_HOME"], "/custom/codex")
        self.assertEqual(environment["MCON_WORKSPACE"], "/workspace")
        self.assertEqual(environment["OTHER"], "value")

    def test_popen_exec_stream_starts_a_new_process_session(self) -> None:
        process = unittest.mock.MagicMock()
        process.stdin = unittest.mock.MagicMock()
        with unittest.mock.patch("mcon.adapters.codex.subprocess.Popen", return_value=process) as popen:
            codex.popen_exec_stream("hello", workspace=Path("/workspace"))

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(process._mcon_process_group, process.pid)


if __name__ == "__main__":
    unittest.main()
