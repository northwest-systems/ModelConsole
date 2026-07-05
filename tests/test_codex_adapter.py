from __future__ import annotations

import json
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
        with (
            unittest.mock.patch("mcon.adapters.codex.subprocess.Popen", return_value=process) as popen,
            unittest.mock.patch(
                "mcon.adapters.codex._prepare_codex_home",
                return_value=Path("/mcon/provider-runtime/codex-test"),
            ),
            unittest.mock.patch(
                "mcon.adapters.codex._codex_runtime_files",
                return_value=[{"action": "edit", "path": "/mcon/provider-runtime/codex-test"}],
            ),
        ):
            codex.popen_exec_stream(
                "hello",
                workspace=Path("/workspace"),
                sandbox_spec={
                    "enabled": True,
                    "workspace": "/workspace",
                    "network": "inherit",
                    "files": [{"action": "read", "path": "/workspace"}],
                },
                mcp_token="token",
                mcp_url="http://127.0.0.1:8765/api/mcp",
            )

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(process._mcon_process_group, process.pid)
        self.assertEqual(
            process._mcon_credential_paths,
            ["/mcon/provider-runtime/codex-test/auth.json"],
        )
        self.assertEqual(popen.call_args.args[0], ["mcon-executor"])
        spec = json.loads(process.stdin.write.call_args.args[0])
        self.assertEqual(spec["stdin"], "hello")
        self.assertEqual(spec["env"]["CODEX_HOME"], "/mcon/provider-runtime/codex-test")
        self.assertEqual(spec["env"]["MCON_MCP_TOKEN"], "token")
        self.assertEqual(spec["sandbox"]["network"], "inherit")
        self.assertEqual(
            spec["sandbox"]["runtime_files"],
            [{"action": "edit", "path": "/mcon/provider-runtime/codex-test"}],
        )
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", spec["argv"])
        self.assertIn("multi_agent", spec["argv"])
        self.assertIn("shell_tool", spec["argv"])
        self.assertIn("unified_exec", spec["argv"])
        self.assertIn('mcp_servers.mcon.url="http://127.0.0.1:8765/api/mcp"', spec["argv"])
        self.assertNotIn("--sandbox", spec["argv"])


if __name__ == "__main__":
    unittest.main()
