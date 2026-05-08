from __future__ import annotations

import unittest
from unittest.mock import patch

from adapter import AdapterError, ClaudeCodeAdapter, CodexAdapter, CopilotAdapter, NvidiaNimAdapter, build_adapter, get_adapter_specs
from adapter.nvidia import _openai_models_to_anthropic_models


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

    # 説明: Codex backend の model list が Codex 用モデルだけを返すことを検証する。
    # 引数: なし。
    # 返り値: なし。
    def test_codex_models_do_not_include_claude_proxy_model(self) -> None:
        with patch.dict("os.environ", {"MCON_CODEX_MODEL": ""}):
            response_payload, status_code = CodexAdapter(default_model="gpt-5.3-codex").request_json("GET", {}, "/v1/models")

        self.assertEqual(status_code, 200)
        self.assertEqual([model["id"] for model in response_payload["data"]], ["gpt-5.3-codex"])

    # 説明: Codex backend が未設定の Claude model を自 backend の model として返さないことを検証する。
    # 引数: なし。
    # 返り値: なし。
    def test_codex_rejects_unavailable_claude_model_lookup(self) -> None:
        with patch.dict("os.environ", {"MCON_CODEX_MODEL": ""}):
            with self.assertRaises(AdapterError) as error_context:
                CodexAdapter(default_model="gpt-5.3-codex").request_json("GET", {}, "/v1/models/claude-sonnet-4-6")

        self.assertEqual(error_context.exception.status_code, 404)

    # 説明: Copilot backend の model list が Copilot 用モデルだけを返すことを検証する。
    # 引数: なし。
    # 返り値: なし。
    def test_copilot_models_do_not_include_claude_proxy_model(self) -> None:
        with patch.dict("os.environ", {"MCON_COPILOT_MODEL": ""}):
            response_payload, status_code = CopilotAdapter(default_model="gpt-5.3-codex").request_json("GET", {}, "/v1/models")

        self.assertEqual(status_code, 200)
        self.assertEqual([model["id"] for model in response_payload["data"]], ["gpt-5.3-codex"])

    # 説明: NVIDIA model list 変換が上流から返った model だけを返すことを検証する。
    # 引数: なし。
    # 返り値: なし。
    def test_nvidia_models_do_not_include_claude_proxy_model(self) -> None:
        response_payload = _openai_models_to_anthropic_models({"data": [{"id": "nvidia/test-model"}]})

        self.assertEqual([model["id"] for model in response_payload["data"]], ["nvidia/test-model"])


if __name__ == "__main__":
    unittest.main()
