from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "mcon" / "src"))

from mcon.policy import PolicyError, PolicyManager


class PolicyManagerTests(unittest.TestCase):
    def test_command_last_matching_permission_wins(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        decision = manager.explain_command("mcon.agent.coder", ["git", "push", "origin", "main"])

        self.assertEqual(decision["final"]["action"], "deny")
        self.assertEqual(
            [item["action"] for item in decision["matched"]],
            ["ask", "deny"],
        )

    def test_git_push_to_feature_branch_asks(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        decision = manager.explain_command("mcon.agent.coder", ["git", "push", "origin", "feature/demo"])

        self.assertEqual(decision["final"]["action"], "ask")

    def test_file_permissions_use_last_matching_rule(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        editable = manager.explain_file("mcon.agent.coder", "write", "/workspace/docs/plan.md")
        denied = manager.explain_file("mcon.agent.coder", "read", "/workspace/secrets/token.txt")
        generated_read = manager.explain_file("mcon.agent.coder", "read", "/workspace/generated/out.txt")
        generated_create = manager.explain_file("mcon.agent.coder", "create", "/workspace/generated/out.txt")

        self.assertTrue(editable["final"]["allowed"])
        self.assertFalse(denied["final"]["allowed"])
        self.assertFalse(generated_read["final"]["allowed"])
        self.assertTrue(generated_create["final"]["allowed"])

    def test_sandbox_spec_uses_resolved_file_permissions(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        spec = manager.sandbox_spec("mcon.agent.coder", session_id="coder-session", session_root="/mcon/session-fs")

        self.assertTrue(spec["enabled"])
        self.assertEqual(spec["workspace"], "/workspace")
        self.assertEqual(spec["network"], "none")
        self.assertEqual(spec["session_id"], "coder-session")
        self.assertEqual(spec["session_root"], "/mcon/session-fs")
        self.assertEqual(
            spec["files"],
            [
                {"action": "read", "path": "/workspace"},
                {"action": "write", "path": "/workspace/generated"},
                {"action": "edit", "path": "/workspace/docs"},
                {"action": "deny", "path": "/workspace/.env"},
                {"action": "deny", "path": "/workspace/secrets"},
            ],
        )

    def test_provider_network_is_allowed_only_for_provider_purpose(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        provider = manager.explain_network("mcon.agent.coder", "inherit", purpose="provider")
        command = manager.explain_network("mcon.agent.coder", "inherit", purpose="command")
        disabled = manager.explain_network("mcon.agent.coder", "none", purpose="command")

        self.assertTrue(provider["final"]["allowed"])
        self.assertFalse(command["final"]["allowed"])
        self.assertTrue(disabled["final"]["allowed"])

    def test_sandbox_spec_rejects_unapproved_network_inheritance(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        with self.assertRaises(PolicyError):
            manager.sandbox_spec(
                "mcon.agent.coder",
                network="inherit",
                network_purpose="command",
            )

    def test_read_only_sandbox_downgrades_edit_and_masks_write_only_paths(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        spec = manager.sandbox_spec(
            "mcon.agent.coder",
            network="inherit",
            network_purpose="provider",
            file_access="read-only",
        )

        self.assertEqual(
            spec["files"],
            [
                {"action": "read", "path": "/workspace"},
                {"action": "deny", "path": "/workspace/generated"},
                {"action": "read", "path": "/workspace/docs"},
                {"action": "deny", "path": "/workspace/.env"},
                {"action": "deny", "path": "/workspace/secrets"},
            ],
        )

    def test_command_file_arguments_block_git_diff_no_index_outside_policy(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        decision = manager.explain_command_file_arguments(
            "mcon.agent.coder",
            ["git", "diff", "--no-index", "/etc/passwd", "/workspace/README.md"],
            cwd="/workspace",
        )

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["violations"][0]["path"], "/etc/passwd")

    def test_file_policy_normalizes_parent_segments_before_matching(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        decision = manager.explain_file("mcon.agent.coder", "read", "/workspace/../etc/passwd")

        self.assertFalse(decision["final"]["allowed"])
        self.assertEqual(decision["path"], "/etc/passwd")

    def test_command_file_arguments_check_paths_before_no_index_option(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        decision = manager.explain_command_file_arguments(
            "mcon.agent.coder",
            ["git", "diff", "/etc/passwd", "--no-index", "/workspace/README.md"],
            cwd="/workspace",
        )

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["violations"][0]["path"], "/etc/passwd")

    def test_command_file_arguments_allow_git_diff_no_index_inside_read_policy(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        decision = manager.explain_command_file_arguments(
            "mcon.agent.coder",
            ["git", "diff", "--no-index", "README.md", "/workspace/docs/plan.md"],
            cwd="/workspace",
        )

        self.assertTrue(decision["allowed"])

    def test_agents_resolve_to_different_permissions(self) -> None:
        manager = PolicyManager.load(Path("configs/plugins/mcon"))

        coder_git = manager.explain_command("mcon.agent.coder", ["git", "status"])
        auditor_git = manager.explain_command("mcon.agent.auditor", ["git", "status"])
        coder_write = manager.explain_file("mcon.agent.coder", "create", "/workspace/generated/out.txt")
        auditor_write = manager.explain_file("mcon.agent.auditor", "create", "/workspace/generated/out.txt")
        auditor_read = manager.explain_file("mcon.agent.auditor", "read", "/workspace/README.md")

        self.assertEqual(coder_git["final"]["action"], "allow")
        self.assertEqual(auditor_git["final"]["action"], "deny")
        self.assertTrue(coder_write["final"]["allowed"])
        self.assertFalse(auditor_write["final"]["allowed"])
        self.assertTrue(auditor_read["final"]["allowed"])

    def test_unknown_policy_reference_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "plugin.toml").write_text(
                textwrap.dedent(
                    """
                    [plugin]
                    name = "mcon"

                    [agent.coder]
                    uses = ["missing"]
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaises(PolicyError):
                PolicyManager.load(root)


if __name__ == "__main__":
    unittest.main()
