from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "mcon" / "src"))

from mcon.adapters.llm_proxy import convert_provider_event


class LlmProxyTests(unittest.TestCase):
    def test_converts_claude_assistant_text_block(self) -> None:
        events = convert_provider_event(
            "claude-code",
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "hello"},
                    ],
                },
            },
        )

        self.assertEqual(events[0]["type"], "llm_event")
        self.assertIn({"type": "assistant_delta", "provider": "claude-code", "delta": "hello"}, events)

    def test_converts_claude_tool_blocks(self) -> None:
        events = convert_provider_event(
            "claude-code",
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "toolu_1", "name": "mcp__mcon__run_command_session", "input": {"argv": ["git", "status"]}},
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok", "is_error": False},
                    ],
                },
            },
        )

        self.assertIn(
            {
                "type": "llm_tool_call",
                "provider": "claude-code",
                "id": "toolu_1",
                "name": "mcp__mcon__run_command_session",
                "input": {"argv": ["git", "status"]},
            },
            events,
        )
        self.assertIn(
            {
                "type": "llm_tool_result",
                "provider": "claude-code",
                "tool_use_id": "toolu_1",
                "content": "ok",
                "is_error": False,
            },
            events,
        )

    def test_converts_claude_result_message(self) -> None:
        events = convert_provider_event(
            "claude-code",
            {
                "type": "result",
                "subtype": "success",
                "result": "done",
                "session_id": "session-1",
                "duration_ms": 42,
                "total_cost_usd": 0.01,
            },
        )

        self.assertIn(
            {
                "type": "llm_result",
                "provider": "claude-code",
                "subtype": "success",
                "result": "done",
                "usage": None,
                "session_id": "session-1",
                "duration_ms": 42,
                "total_cost_usd": 0.01,
            },
            events,
        )

    def test_converts_claude_partial_text_delta(self) -> None:
        events = convert_provider_event(
            "claude-code",
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "tok"},
                },
            },
        )

        self.assertIn({"type": "assistant_delta", "provider": "claude-code", "delta": "tok"}, events)
        self.assertEqual(events[-1]["type"], "llm_stream_event")

    def test_preserves_unknown_provider_event_without_text(self) -> None:
        events = convert_provider_event("unknown", {"type": "vendor_event", "payload": {"x": 1}})

        self.assertEqual(events, [{"type": "llm_event", "provider": "unknown", "message": {"type": "vendor_event", "payload": {"x": 1}}}])

    def test_converts_codex_delta_to_llm_proxy_delta(self) -> None:
        events = convert_provider_event("codex", {"type": "agent_message_delta", "delta": "token"})

        self.assertEqual(events[0]["type"], "llm_event")
        self.assertIn({"type": "assistant_delta", "provider": "codex", "delta": "token"}, events)


if __name__ == "__main__":
    unittest.main()
