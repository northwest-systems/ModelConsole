from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = str(ROOT / "packages" / "mcon" / "src")
PLUGIN_ROOT = str(ROOT / "configs" / "plugins" / "mcon")


@unittest.skipUnless(os.environ.get("MCON_ENABLE_SOCKET_TESTS") == "1", "socket bind integration tests are opt-in")
class ServerApiTests(unittest.TestCase):
    def test_run_api_sync_in_and_diff_over_http(self) -> None:
        with running_server() as server:
            (server.workspace / "README.md").write_text("before\n", encoding="utf-8")
            (server.workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

            created = post_json(server.url("/api/runs"), {"subject": "mcon.agent.coder"})
            synced = post_json(server.url(f"/api/runs/{created['run_id']}/sync-in"), {})
            agent_workspace = Path(synced["agent_workspace"])
            (agent_workspace / "README.md").write_text("after\n", encoding="utf-8")
            diff = post_json(server.url(f"/api/runs/{created['run_id']}/diff"), {})

            self.assertEqual(synced["state"], "READY")
            self.assertFalse((agent_workspace / ".env").exists())
            self.assertEqual(diff["changed"], ["README.md"])
            self.assertIn("-before", diff["patch"])
            self.assertIn("+after", diff["patch"])

    def test_run_api_diff_quarantines_secret_output_over_http(self) -> None:
        with running_server() as server:
            (server.workspace / "README.md").write_text("before\n", encoding="utf-8")
            created = post_json(server.url("/api/runs"), {"subject": "mcon.agent.coder"})
            synced = post_json(server.url(f"/api/runs/{created['run_id']}/sync-in"), {})
            agent_workspace = Path(synced["agent_workspace"])
            (agent_workspace / "leak.txt").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")

            diff = post_json(server.url(f"/api/runs/{created['run_id']}/diff"), {})

            self.assertEqual(diff["state"], "QUARANTINED")
            self.assertEqual(diff["patch"], "")
            self.assertIn("secret material detected", diff["quarantine_reason"])


class running_server:
    def __enter__(self) -> "running_server":
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.run_root = self.root / "runs"
        self.session_root = self.root / "session-fs"
        self.workspace.mkdir()
        env = {
            **os.environ,
            "PYTHONPATH": PYTHONPATH,
            "PYTHONUNBUFFERED": "1",
            "MCON_WORKSPACE": str(self.workspace),
            "MCON_RUN_ROOT": str(self.run_root),
            "MCON_SESSION_ROOT": str(self.session_root),
        }
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mcon",
                "--plugin-root",
                PLUGIN_ROOT,
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert self.process.stdout is not None
        line = read_line_with_timeout(self.process.stdout, timeout_seconds=5)
        prefix = "mcon server listening on "
        if not line.startswith(prefix):
            stderr = ""
            if self.process.stderr is not None:
                stderr = self.process.stderr.read()
            raise AssertionError(f"server did not report listen address: {line!r}; stderr={stderr!r}")
        self.base_url = line.removeprefix(prefix).strip()
        wait_for_health(self.base_url)
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()
        self.temporary_directory.cleanup()

    def url(self, path: str) -> str:
        return self.base_url + path


def read_line_with_timeout(stream: object, *, timeout_seconds: float) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        ready, _, _ = select.select([stream], [], [], 0.1)
        if ready:
            return stream.readline()
    raise AssertionError("server did not start before timeout")


def wait_for_health(base_url: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("server health check did not pass")


def post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
