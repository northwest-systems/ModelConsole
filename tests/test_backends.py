from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from config.backends import (
    get_auth_label,
    get_container_environment_names,
    get_default_model,
    normalize_backend_name,
)


class BackendRegistryTests(unittest.TestCase):
    def test_normalizes_backend_aliases(self) -> None:
        """backend alias が Python 側 registry で正規化されることを検証する。"""

        self.assertEqual(normalize_backend_name("claude"), "claude-code")
        self.assertEqual(normalize_backend_name("nvidia-nim"), "nvidia")

    def test_claude_code_defaults_to_oauth_label(self) -> None:
        """Claude Code backend の未認証表示が OAuth login を案内することを検証する。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            label = get_auth_label(Path(temporary_directory), "claude-code")

        self.assertIn("mcon login claude --method oauth", label)

    def test_container_environment_names_come_from_registry(self) -> None:
        """container に渡す provider 環境変数が registry から組み立てられることを検証する。"""

        environment_names = get_container_environment_names()

        self.assertIn("ANTHROPIC_API_KEY", environment_names)
        self.assertIn("MCON_CLAUDE_CODE_MODEL", environment_names)
        self.assertIn("MCON_NVIDIA_BASE_URL", environment_names)

    def test_backend_specific_model_environment_overrides_default(self) -> None:
        """backend 固有 model 環境変数が標準 model を上書きすることを検証する。"""

        previous_global_value = os.environ.get("MCON_BACKEND_MODEL")
        previous_backend_value = os.environ.get("MCON_CLAUDE_CODE_MODEL")
        try:
            os.environ.pop("MCON_BACKEND_MODEL", None)
            os.environ["MCON_CLAUDE_CODE_MODEL"] = "custom-claude-model"
            self.assertEqual(get_default_model("claude-code"), "custom-claude-model")
        finally:
            if previous_global_value is None:
                os.environ.pop("MCON_BACKEND_MODEL", None)
            else:
                os.environ["MCON_BACKEND_MODEL"] = previous_global_value
            if previous_backend_value is None:
                os.environ.pop("MCON_CLAUDE_CODE_MODEL", None)
            else:
                os.environ["MCON_CLAUDE_CODE_MODEL"] = previous_backend_value


if __name__ == "__main__":
    unittest.main()
