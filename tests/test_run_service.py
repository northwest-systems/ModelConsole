from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "mcon" / "src"))

from mcon.run import RunService, RunState, secret_path_reason


class RunServiceTests(unittest.TestCase):
    def test_sync_in_excludes_secret_like_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "real"
            workspace.mkdir()
            (workspace / "src").mkdir()
            (workspace / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            (workspace / ".npmrc").write_text("//registry/:_authToken=secret\n", encoding="utf-8")
            (workspace / ".ssh").mkdir()
            (workspace / ".ssh" / "id_rsa").write_text("secret\n", encoding="utf-8")
            (workspace / "secrets").mkdir()
            (workspace / "secrets" / "token.txt").write_text("secret\n", encoding="utf-8")

            service = RunService(root=root / "runs", default_workspace=workspace)
            run = service.create_run(
                subject="mcon.agent.coder",
                policy_snapshot={"policies": ["workspace"]},
            )
            synced = service.sync_in(run.run_id)

            self.assertEqual(synced.state, RunState.READY)
            self.assertTrue((synced.agent_workspace / "src" / "app.py").exists())
            self.assertFalse((synced.agent_workspace / ".env").exists())
            self.assertFalse((synced.agent_workspace / ".npmrc").exists())
            self.assertFalse((synced.agent_workspace / ".ssh").exists())
            self.assertFalse((synced.agent_workspace / "secrets").exists())

    def test_agent_workspace_quarantines_if_secret_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "real"
            workspace.mkdir()
            (workspace / "src.py").write_text("AWS_SECRET_ACCESS_KEY = 'x'\n", encoding="utf-8")
            service = RunService(root=root / "runs", default_workspace=workspace)
            run = service.create_run(
                subject="mcon.agent.coder",
                policy_snapshot={"policies": ["workspace"]},
            )

            synced = service.sync_in(run.run_id)

            self.assertEqual(synced.state, RunState.QUARANTINED)
            self.assertIn("secret material detected", synced.quarantine_reason or "")

    def test_diff_reports_agent_workspace_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "real"
            workspace.mkdir()
            (workspace / "README.md").write_text("before\n", encoding="utf-8")
            service = RunService(root=root / "runs", default_workspace=workspace)
            run = service.create_run(
                subject="mcon.agent.coder",
                policy_snapshot={"policies": ["workspace"]},
            )
            service.sync_in(run.run_id)
            (workspace / "README.md").write_text("drifted\n", encoding="utf-8")
            (run.agent_workspace / "README.md").write_text("after\n", encoding="utf-8")

            diff = service.diff(run.run_id)

            self.assertEqual(diff["changed"], ["README.md"])
            self.assertIn("--- a/README.md", diff["patch"])
            self.assertIn("+++ b/README.md", diff["patch"])
            self.assertIn("-before", diff["patch"])
            self.assertIn("+after", diff["patch"])
            self.assertNotIn("drifted", diff["patch"])

    def test_secret_path_reason_matches_recursive_secret_paths(self) -> None:
        self.assertEqual(secret_path_reason(Path("nested/.env.local")), "environment file")
        self.assertEqual(secret_path_reason(Path("pkg/.aws/config")), "secret directory")
        self.assertEqual(secret_path_reason(Path("docs/credential-notes.md")), "secret-like path")

    def test_diff_quarantines_if_agent_writes_secret_like_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "real"
            workspace.mkdir()
            (workspace / "README.md").write_text("before\n", encoding="utf-8")
            service = RunService(root=root / "runs", default_workspace=workspace)
            run = service.create_run(
                subject="mcon.agent.coder",
                policy_snapshot={"policies": ["workspace"]},
            )
            service.sync_in(run.run_id)
            (run.agent_workspace / "README.md").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")

            diff = service.diff(run.run_id)

            self.assertEqual(diff["state"], RunState.QUARANTINED)
            self.assertEqual(diff["patch"], "")
            self.assertIn("secret material detected", diff["quarantine_reason"])


if __name__ == "__main__":
    unittest.main()
