from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "mcon" / "src"))

from mcon.server.app import _prompt_from_messages
from mcon.tui.client import TuiState, _backspace, _delete_at_cursor, _delete_previous_word, _display_width, _event_text, _new_chat_id


class TuiTests(unittest.TestCase):
    def test_prompt_from_messages_includes_transcript(self) -> None:
        prompt = _prompt_from_messages(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
        )

        self.assertIn("Transcript:", prompt)
        self.assertIn("USER: hello", prompt)
        self.assertIn("ASSISTANT: hi", prompt)

    def test_event_text_extracts_codex_delta(self) -> None:
        text = _event_text({"type": "codex_event", "event": {"type": "agent_message_delta", "delta": "token"}})

        self.assertEqual(text, "token")

    def test_event_text_extracts_codex_exec_completed_agent_message(self) -> None:
        text = _event_text(
            {
                "type": "codex_event",
                "event": {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "agent_message", "text": "pong"},
                },
            }
        )

        self.assertEqual(text, "pong")

    def test_event_text_extracts_stdout_fallback(self) -> None:
        text = _event_text({"type": "stdout", "text": "plain"})

        self.assertEqual(text, "plain")

    def test_new_chat_id_uses_hash_and_five_digits(self) -> None:
        chat_id = _new_chat_id()

        self.assertRegex(chat_id, r"^#[0-9]{5}$")

    def test_line_editor_deletes_around_cursor(self) -> None:
        state = TuiState(server_url="http://127.0.0.1:8765")
        state.input_buffer = list("abc")
        state.input_cursor = 2

        _backspace(state)
        self.assertEqual("".join(state.input_buffer), "ac")
        self.assertEqual(state.input_cursor, 1)

        _delete_at_cursor(state)
        self.assertEqual("".join(state.input_buffer), "a")
        self.assertEqual(state.input_cursor, 1)

    def test_line_editor_deletes_previous_word(self) -> None:
        state = TuiState(server_url="http://127.0.0.1:8765")
        state.input_buffer = list("alpha beta  ")
        state.input_cursor = len(state.input_buffer)

        _delete_previous_word(state)

        self.assertEqual("".join(state.input_buffer), "alpha ")
        self.assertEqual(state.input_cursor, len("alpha "))

    def test_display_width_counts_wide_characters(self) -> None:
        self.assertEqual(_display_width("abc"), 3)
        self.assertEqual(_display_width("テスト"), 6)


if __name__ == "__main__":
    unittest.main()
