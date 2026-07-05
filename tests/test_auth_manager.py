from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "mcon" / "src"))

from mcon.auth.manager import AuthManager, ProviderSpec, _clean_terminal_output


class AuthManagerTests(unittest.TestCase):
    def test_device_login_job_captures_output(self) -> None:
        manager = AuthManager(
            {
                "fake": ProviderSpec(
                    name="fake",
                    device_login_command=("/bin/sh", "-c", "printf 'device-code: 1234\\n'"),
                    status_command=("/bin/sh", "-c", "printf authenticated"),
                    environment=dict(os.environ),
                    cwd=Path("."),
                )
            }
        )

        started = manager.start_device_login("fake")
        deadline = time.time() + 5
        snapshot = started
        while time.time() < deadline:
            snapshot = manager.get_device_login("fake", started["id"])
            if snapshot["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.05)

        self.assertEqual(snapshot["status"], "succeeded")
        self.assertIn("device-code: 1234", snapshot["output"])

    def test_status_command_reports_authenticated_on_zero_exit(self) -> None:
        manager = AuthManager(
            {
                "fake": ProviderSpec(
                    name="fake",
                    device_login_command=("/bin/true",),
                    status_command=("/bin/sh", "-c", "printf authenticated"),
                    environment=dict(os.environ),
                    cwd=Path("."),
                )
            }
        )

        status = manager.status("fake")

        self.assertTrue(status["authenticated"])
        self.assertEqual(status["status"], "authenticated")

    def test_clean_terminal_output_removes_ansi_sequences(self) -> None:
        self.assertEqual(_clean_terminal_output("\r\n\u001b[94mCODE\u001b[0m\r\n"), "\nCODE\n")


if __name__ == "__main__":
    unittest.main()
