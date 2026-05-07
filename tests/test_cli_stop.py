from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli import _cmd_stop
from config import initialize_data_directory
from runtime import read_runtime, write_runtime


class CliStopTests(unittest.TestCase):
    def test_stop_clears_dead_server_runtime(self) -> None:
        """記録済み server pid が存在しない場合に runtime を停止状態へ戻すことを検証する。

        Args:
            なし。

        Returns:
            なし。
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            data_directory = Path(temporary_directory)
            initialize_data_directory(data_directory)
            runtime_path = data_directory / "runtime.json"
            write_runtime(
                runtime_path,
                {
                    "server_pid": 999999,
                    "server_url": "http://127.0.0.1:8765",
                    "server_host": "127.0.0.1",
                    "server_port": 8765,
                },
            )

            with patch("cli._pid_is_running", return_value=False):
                exit_code = _cmd_stop(data_directory)

            runtime_data = read_runtime(runtime_path)
            self.assertEqual(exit_code, 0)
            self.assertIsNone(runtime_data["server_pid"])
            self.assertIsNone(runtime_data["server_url"])


if __name__ == "__main__":
    unittest.main()
