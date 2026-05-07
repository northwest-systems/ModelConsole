from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import initialize_data_directory, load_config
from system import SystemRuntime


class SystemRuntimeTests(unittest.TestCase):
    def test_manages_multiple_passthrough_sessions(self) -> None:
        """複数 session が transcript を圧縮せず横流しできることを検証する。

        Args:
            なし。

        Returns:
            なし。
        """

        captured_requests = []

        class FakeAdapter:
            def forward_json(self, request_headers: object, request_payload: dict, upstream_path: str = "/v1/messages") -> tuple[dict, int]:
                """system/middleware から受け取った request を記録する fake adapter。

                Args:
                    request_headers: adapter に渡された headers。
                    request_payload: adapter に渡された Anthropic-like payload。
                    upstream_path: adapter に渡された upstream path。

                Returns:
                    fake response payload と status code。
                """

                captured_requests.append(request_payload)
                return {
                    "content": [{"type": "text", "text": f"reply {len(captured_requests)}"}],
                    "usage": {"input_tokens": len(request_payload["messages"]), "output_tokens": 2},
                }, 200

        with tempfile.TemporaryDirectory() as temporary_directory:
            data_directory = Path(temporary_directory)
            initialize_data_directory(data_directory)
            runtime_config = load_config(data_directory)
            system_runtime = SystemRuntime(runtime_config)

            with patch("adapter.build_adapter", return_value=FakeAdapter()):
                first_session = system_runtime.create_session(title="one", backend="codex")
                second_session = system_runtime.create_session(title="two", backend="nvidia")
                first_result = system_runtime.append_user_message(first_session["id"], "hello")
                system_runtime.append_user_message(second_session["id"], "separate")
                second_result = system_runtime.append_user_message(first_session["id"], "continue")

            self.assertNotEqual(first_session["id"], second_session["id"])
            self.assertEqual(first_result["assistant_message"]["content"], "reply 1")
            self.assertEqual(second_result["assistant_message"]["content"], "reply 3")
            self.assertEqual([message["role"] for message in captured_requests[2]["messages"]], ["user", "assistant", "user"])
            self.assertEqual(captured_requests[2]["messages"][0]["content"], "hello")
            self.assertEqual(captured_requests[2]["messages"][1]["content"], "reply 1")
            self.assertEqual(captured_requests[2]["messages"][2]["content"], "continue")


if __name__ == "__main__":
    unittest.main()
