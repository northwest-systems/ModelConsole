from __future__ import annotations

import json
import signal
import sys
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "mcon" / "src"))

from mcon.policy import PolicyManager
from mcon.server.app import (
    _agent_scoped_session_id,
    _chat_id,
    _chat_sandbox,
    _executor_command,
    _handle_mcp_request,
    _policy_context,
    _prompt_from_messages,
    _required_messages,
    _scrub_process_credentials,
    _stream_process_as_ndjson,
    _terminate_process_group,
)


class ServerTests(unittest.TestCase):
    def test_session_id_is_scoped_by_agent_subject(self) -> None:
        coder = _agent_scoped_session_id("mcon.agent.coder", "session-1")
        auditor = _agent_scoped_session_id("mcon.agent.auditor", "session-1")

        self.assertNotEqual(coder, auditor)
        self.assertEqual(coder, "mcon_agent_coder--session-1")
        self.assertEqual(auditor, "mcon_agent_auditor--session-1")

    def test_required_messages_replaces_unpaired_surrogates(self) -> None:
        messages = _required_messages({"messages": [{"role": "user", "content": "bad\udcfftext"}]})

        self.assertEqual(messages, [{"role": "user", "content": "bad\ufffdtext"}])
        messages[0]["content"].encode("utf-8")

    def test_required_messages_preserves_chat_id(self) -> None:
        messages = _required_messages({"messages": [{"role": "user", "content": "hello", "chat_id": "#01234"}]})

        self.assertEqual(messages, [{"role": "user", "content": "hello", "chat_id": "#01234"}])

    def test_required_messages_rejects_invalid_chat_id(self) -> None:
        with self.assertRaises(ValueError):
            _required_messages({"messages": [{"role": "user", "content": "hello", "chat_id": "bad\nid"}]})

    def test_prompt_includes_chat_id_and_policy_context(self) -> None:
        prompt = _prompt_from_messages(
            [{"role": "user", "content": "hello", "chat_id": "#01234"}],
            chat_id="#01234",
            policy_context="subject: mcon.agent.coder",
        )

        self.assertIn("Current chat id: #01234", prompt)
        self.assertIn("Reply to the latest USER message for #01234.", prompt)
        self.assertIn("ModelConsole policy context:", prompt)
        self.assertIn("USER #01234: hello", prompt)
        self.assertIn("The built-in shell is disabled.", prompt)
        self.assertIn("run_command_session", prompt)

    def test_chat_id_uses_payload_or_last_message(self) -> None:
        self.assertEqual(_chat_id({"chat_id": "#99999"}, []), "#99999")
        self.assertEqual(_chat_id({}, [{"role": "user", "content": "hello", "chat_id": "#01234"}]), "#01234")

    def test_chat_sandbox_rejects_workspace_write(self) -> None:
        with self.assertRaises(ValueError):
            _chat_sandbox({"sandbox": "workspace-write"})

    def test_policy_context_describes_resolved_policy(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))
        context = _policy_context(manager.resolve_subject("mcon.agent.coder"), cwd=Path("/workspace"), sandbox="read-only")

        self.assertIn("subject: mcon.agent.coder", context)
        self.assertIn("command_policy: last matching permission wins", context)
        self.assertIn("mcon.policy.git.commands.git-push: action=ask", context)
        self.assertIn("file_actions: deny=no access; read=stat/list/read; write=create only; edit=stat/list/read/create/write.", context)
        self.assertIn("mcon.policy.provider-network.network.provider-api: action=allow", context)

    def test_stream_process_terminates_on_write_failure(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import json, time; print(json.dumps({'type': 'agent_message_delta', 'delta': 'x'}), flush=True); time.sleep(30)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        process._mcon_process_group = process.pid  # type: ignore[attr-defined]

        def fail_write(_event: object) -> None:
            raise BrokenPipeError("client disconnected")

        try:
            with self.assertRaises(BrokenPipeError):
                _stream_process_as_ndjson(process, fail_write)
        finally:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

        self.assertIsNotNone(process.poll())

    def test_terminate_process_group_kills_surviving_children(self) -> None:
        process = unittest.mock.MagicMock()
        process.poll.side_effect = [None, 0, 0]

        with unittest.mock.patch("mcon.server.app.os.killpg") as killpg:
            _terminate_process_group(process, 1234)

        self.assertEqual(
            killpg.call_args_list,
            [
                unittest.mock.call(1234, signal.SIGTERM),
                unittest.mock.call(1234, signal.SIGKILL),
            ],
        )

    def test_terminate_process_removes_provider_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime_dir = Path(root) / "provider"
            runtime_dir.mkdir()
            process = unittest.mock.MagicMock()
            process.poll.return_value = 0
            process._mcon_process_group = None
            process._mcon_runtime_dir = str(runtime_dir)

            from mcon.server.app import _terminate_process

            _terminate_process(process)

            self.assertFalse(runtime_dir.exists())

    def test_scrub_process_credentials_removes_auth_copy(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            auth_path = Path(root) / "auth.json"
            auth_path.write_text("secret", encoding="utf-8")
            process = unittest.mock.MagicMock()
            process._mcon_credential_paths = [str(auth_path)]

            _scrub_process_credentials(process)

            self.assertFalse(auth_path.exists())
            self.assertEqual(process._mcon_credential_paths, [])

    def test_mcp_lists_only_command_session_tool(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        response = _handle_mcp_request(
            manager,
            {
                "subject": "mcon.agent.coder",
                "parent_session_id": "parent",
                "cwd": "/workspace",
            },
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

        self.assertEqual(response["result"]["tools"][0]["name"], "run_command_session")

    def test_mcp_command_session_applies_caller_command_policy(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        response = _handle_mcp_request(
            manager,
            {
                "subject": "mcon.agent.auditor",
                "parent_session_id": "parent",
                "cwd": "/workspace",
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "run_command_session",
                    "arguments": {"argv": ["git", "status"]},
                },
            },
        )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("command blocked", response["result"]["content"][0]["text"])

    def test_mcp_command_session_runs_in_separate_executor_process(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))
        completed = subprocess.CompletedProcess(
            args=_executor_command(),
            returncode=0,
            stdout="ok",
            stderr="",
        )
        with unittest.mock.patch("mcon.server.app.subprocess.run", return_value=completed) as run:
            response = _handle_mcp_request(
                manager,
                {
                    "subject": "mcon.agent.coder",
                    "parent_session_id": "parent",
                    "cwd": "/workspace",
                },
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "run_command_session",
                        "arguments": {"argv": ["git", "status"]},
                    },
                },
            )

        self.assertFalse(response["result"]["isError"])
        structured = response["result"]["structuredContent"]
        self.assertTrue(structured["session_id"].startswith("parent--tool-"))
        spec = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(run.call_args.args[0], _executor_command())
        self.assertEqual(spec["sandbox"]["network"], "none")
        self.assertEqual(spec["argv"], ["git", "status"])


if __name__ == "__main__":
    unittest.main()
