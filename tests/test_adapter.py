from __future__ import annotations

import unittest
from unittest.mock import patch

from adapter import ClaudeCodeAdapter, NvidiaNimAdapter, build_adapter, get_adapter_specs


class AdapterFactoryTests(unittest.TestCase):
    # 説明: adapter module の自己登録情報から factory が backend 一覧を組み立てることを検証する。
    # 引数: なし。
    # 返り値: なし。
    def test_discovers_adapter_specs_from_modules(self) -> None:
        adapter_specs = get_adapter_specs()
        self.assertIn("claude-code", adapter_specs)
        self.assertIn("nvidia", adapter_specs)
        self.assertIsInstance(build_adapter("nim"), NvidiaNimAdapter)

    # 説明: claude-code backend が Claude Code adapter を返すことを検証する。
    # 引数: なし。
    # 返り値: なし。
    def test_builds_claude_code_adapter(self) -> None:
        self.assertIsInstance(build_adapter("claude-code"), ClaudeCodeAdapter)
        self.assertIsInstance(build_adapter("claude"), ClaudeCodeAdapter)

    # 説明: Claude Code adapter が claude CLI を呼ぶことを検証する。
    # 引数: なし。
    # 返り値: なし。
    def test_claude_code_adapter_runs_claude_cli(self) -> None:
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
