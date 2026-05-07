from __future__ import annotations

import unittest

from middleware import MIDDLEWARE_BYPASS, MiddlewareRequest, build_middleware


class MiddlewareTests(unittest.TestCase):
    def test_bypass_middleware_forwards_request_unchanged(self) -> None:
        """bypass middleware が request を変更せず adapter へ渡すことを検証する。

        Args:
            なし。

        Returns:
            なし。
        """

        captured = {}

        class FakeAdapter:
            def forward_json(
                self,
                request_headers: object,
                request_payload: dict,
                upstream_path: str = "/v1/messages",
            ) -> tuple[dict, int]:
                """middleware から渡された値を記録する fake adapter。

                Args:
                    request_headers: middleware から渡された headers。
                    request_payload: middleware から渡された payload。
                    upstream_path: middleware から渡された upstream path。

                Returns:
                    fake response payload と status code。
                """

                captured["headers"] = request_headers
                captured["payload"] = request_payload
                captured["upstream_path"] = upstream_path
                return {"content": [{"type": "text", "text": "ok"}]}, 200

        request = MiddlewareRequest(
            session_id="session-1",
            backend="codex",
            model="gpt-5.3-codex",
            payload={"model": "gpt-5.3-codex", "messages": [{"role": "user", "content": "hello"}]},
            headers={"x-test": "1"},
        )

        response = build_middleware().forward(request, FakeAdapter())

        self.assertEqual(response.context_mode, MIDDLEWARE_BYPASS)
        self.assertEqual(response.status_code, 200)
        self.assertIs(captured["payload"], request.payload)
        self.assertIs(captured["headers"], request.headers)
        self.assertEqual(captured["upstream_path"], "/v1/messages")


if __name__ == "__main__":
    unittest.main()
