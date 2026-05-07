from __future__ import annotations

import unittest
from unittest.mock import patch

from adapter import ClaudeCodeAdapter, build_adapter


class AdapterFactoryTests(unittest.TestCase):
    def test_builds_claude_code_adapter(self) -> None:
        """claude-code backend が Claude Code adapter を返すことを検証する。

        Args:
            なし。

        Returns:
            なし。
        """

        self.assertIsInstance(build_adapter("claude-code"), ClaudeCodeAdapter)
        self.assertIsInstance(build_adapter("claude"), ClaudeCodeAdapter)

    def test_claude_code_adapter_runs_claude_cli(self) -> None:
        """Claude Code adapter が claude CLI を呼ぶことを検証する。

        Args:
            なし。

        Returns:
            なし。
        """

        class CompletedProcess:
            returncode = 0
            stdout = "hello"
            stderr = ""

        with patch("adapter.claude_code.subprocess.run", return_value=CompletedProcess()) as run_mock:
            response_payload, status_code = ClaudeCodeAdapter(default_model="claude-sonnet-4-6").forward_json(
                {},
                {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hi"}]},
            )

        command = run_mock.call_args.args[0]
        self.assertEqual(command[:2], ["claude", "-p"])
        self.assertIn("--model", command)
        self.assertEqual(status_code, 200)
        self.assertEqual(response_payload["content"][0]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
