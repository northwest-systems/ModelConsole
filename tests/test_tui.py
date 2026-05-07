from __future__ import annotations

import unittest
from unittest.mock import patch

from tui import _ensure_server, _ensure_session


class TuiSessionSelectionTests(unittest.TestCase):
    def test_ensure_server_starts_persistent_server_process(self) -> None:
        """healthy server がない場合に persistent server process を起動することを検証する。

        Args:
            なし。

        Returns:
            なし。
        """

        class RuntimeConfig:
            data_directory = "/tmp/mcon-test"
            runtime_path = "/tmp/mcon-test/runtime.json"

        class FakeProcess:
            def poll(self) -> None:
                """process が起動中であることを返す。

                Args:
                    なし。

                Returns:
                    なし。
                """

                return None

        with patch("tui.read_runtime", side_effect=[{}, {"server_url": "http://127.0.0.1:8765"}]), patch(
            "tui._server_is_healthy", side_effect=[True]
        ), patch("tui.subprocess.Popen", return_value=FakeProcess()) as popen_mock:
            server_url = _ensure_server(RuntimeConfig())

        self.assertEqual(server_url, "http://127.0.0.1:8765")
        popen_mock.assert_called_once()
        self.assertTrue(popen_mock.call_args.kwargs["start_new_session"])

    def test_backend_boot_creates_session_when_existing_session_uses_other_backend(self) -> None:
        """backend 指定 boot が別 backend の既存 session を再利用しないことを検証する。

        Args:
            なし。

        Returns:
            なし。
        """

        created_session = {
            "id": "nvidia-session",
            "backend": "nvidia",
            "model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
            "title": "session-nvidia",
        }

        def fake_get(server_url: str, path: str) -> dict:
            """TUI API GET の fake response を返す。

            Args:
                server_url: 呼び出し先 server URL。
                path: 呼び出し path。

            Returns:
                fake JSON response。
            """

            self.assertEqual(server_url, "http://127.0.0.1:8765")
            self.assertEqual(path, "/api/sessions")
            return {
                "sessions": [
                    {
                        "id": "codex-session",
                        "backend": "codex",
                        "model": "gpt-5.3-codex",
                        "title": "session-codex",
                    }
                ]
            }

        def fake_post(server_url: str, path: str, payload: dict) -> dict:
            """TUI API POST の fake response を返す。

            Args:
                server_url: 呼び出し先 server URL。
                path: 呼び出し path。
                payload: POST payload。

            Returns:
                fake JSON response。
            """

            self.assertEqual(server_url, "http://127.0.0.1:8765")
            self.assertEqual(path, "/api/sessions")
            self.assertEqual(payload["backend"], "nvidia")
            return {"session": created_session}

        with patch("tui._api_get", side_effect=fake_get), patch("tui._api_post", side_effect=fake_post):
            session = _ensure_session(
                "http://127.0.0.1:8765",
                session_id=None,
                backend="nvidia",
                model=None,
                title=None,
            )

        self.assertEqual(session, created_session)

    def test_backend_boot_reuses_matching_backend_session(self) -> None:
        """backend 指定 boot が同じ backend の既存 session を再利用することを検証する。

        Args:
            なし。

        Returns:
            なし。
        """

        nvidia_session = {
            "id": "nvidia-session",
            "backend": "nvidia",
            "model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
            "title": "session-nvidia",
        }

        with patch(
            "tui._api_get",
            return_value={
                "sessions": [
                    {"id": "codex-session", "backend": "codex", "model": "gpt-5.3-codex", "title": "session-codex"},
                    nvidia_session,
                ]
            },
        ), patch("tui._api_post") as post_mock:
            session = _ensure_session(
                "http://127.0.0.1:8765",
                session_id=None,
                backend="nvidia",
                model=None,
                title=None,
            )

        post_mock.assert_not_called()
        self.assertEqual(session, nvidia_session)


if __name__ == "__main__":
    unittest.main()
