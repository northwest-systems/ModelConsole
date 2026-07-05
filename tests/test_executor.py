from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "mcon" / "src"))

from mcon.executor import (
    ExecSpec,
    FileRule,
    SandboxSpec,
    apply_session_writes,
    build_command,
    parse_exec_spec,
    run,
    session_write_source,
)


class ExecutorTests(unittest.TestCase):
    def test_run_executes_command_with_provided_env(self) -> None:
        payload = json.dumps(
            {
                "argv": [sys.executable, "-c", "import os; print(os.environ['MCON_TEST'], end='')"],
                "env": {"MCON_TEST": "ok"},
            }
        )
        with tempfile.TemporaryFile("w+", encoding="utf-8") as stdout, tempfile.TemporaryFile("w+", encoding="utf-8") as stderr:
            run(stdin=_StringReader(payload), stdout=stdout, stderr=stderr)
            stdout.seek(0)
            self.assertEqual(stdout.read(), "ok")

    def test_run_passes_provided_stdin_to_command(self) -> None:
        payload = json.dumps({"argv": [sys.executable, "-c", "import sys; print(sys.stdin.read(), end='')"], "stdin": "hello"})
        with tempfile.TemporaryFile("w+", encoding="utf-8") as stdout, tempfile.TemporaryFile("w+", encoding="utf-8") as stderr:
            run(stdin=_StringReader(payload), stdout=stdout, stderr=stderr)
            stdout.seek(0)
            self.assertEqual(stdout.read(), "hello")

    def test_run_rejects_empty_argv(self) -> None:
        with tempfile.TemporaryFile("w+", encoding="utf-8") as stdout, tempfile.TemporaryFile("w+", encoding="utf-8") as stderr:
            with self.assertRaises(ValueError):
                run(stdin=_StringReader('{"argv":[]}'), stdout=stdout, stderr=stderr)

    def test_build_command_wraps_sandboxed_command_with_bubblewrap(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_root, tempfile.TemporaryDirectory() as session_root:
            workspace = Path(workspace_root)
            secrets_path = workspace / "secrets"
            docs_path = workspace / "docs"
            generated_path = workspace / "generated"
            secrets_path.mkdir()
            docs_path.mkdir()
            generated_path.mkdir()
            spec = ExecSpec(
                argv=["/usr/bin/env"],
                cwd=str(docs_path),
                env={"PATH": "/usr/bin:/bin"},
                sandbox=SandboxSpec(
                    enabled=True,
                    workspace=str(workspace),
                    network="none",
                    session_id="test-session",
                    session_root=session_root,
                    files=[
                        FileRule("read", str(workspace)),
                        FileRule("deny", str(secrets_path)),
                        FileRule("edit", str(docs_path)),
                        FileRule("write", str(generated_path)),
                    ],
                    runtime_files=[
                        FileRule("read", "/mcon/codex-home/packages"),
                        FileRule("read", "/etc/resolv.conf"),
                    ],
                ),
            )

            command = build_command(spec)

            self.assertEqual(command.argv[0], "bwrap")
            joined = " ".join(command.argv)
            expected_parts = [
                "--unshare-net",
                f"--ro-bind-try {workspace} {workspace}",
                f"--tmpfs {secrets_path}",
                f"--bind-try {docs_path} {docs_path}",
                f"--bind {Path(session_root) / 'test-session' / 'fs' / 'generated'} {generated_path}",
                "--ro-bind-try /mcon/codex-home/packages /mcon/codex-home/packages",
                "--ro-bind-try /etc/resolv.conf /etc/resolv.conf",
                f"--chdir {docs_path} -- /usr/bin/env",
            ]
            for expected in expected_parts:
                self.assertIn(expected, joined)
            self.assertNotIn("--ro-bind /etc /etc", joined)

    def test_build_command_rejects_runtime_path_outside_allowlist(self) -> None:
        spec = ExecSpec(
            argv=["/bin/true"],
            cwd="/workspace",
            sandbox=SandboxSpec(enabled=True, workspace="/workspace", runtime_files=[FileRule("read", "/root")]),
        )

        with self.assertRaises(ValueError):
            build_command(spec)

    def test_build_command_rejects_writable_runtime_credential_mount(self) -> None:
        spec = ExecSpec(
            argv=["/bin/true"],
            cwd="/workspace",
            sandbox=SandboxSpec(enabled=True, workspace="/workspace", runtime_files=[FileRule("write", "/mcon/codex-home")]),
        )

        with self.assertRaises(ValueError):
            build_command(spec)

    def test_build_command_rejects_reading_codex_credential_home(self) -> None:
        spec = ExecSpec(
            argv=["/bin/true"],
            cwd="/workspace",
            sandbox=SandboxSpec(
                enabled=True,
                workspace="/workspace",
                runtime_files=[FileRule("read", "/mcon/codex-home/auth.json")],
            ),
        )

        with self.assertRaises(ValueError):
            build_command(spec)

    def test_build_command_rejects_writable_etc_runtime_mount(self) -> None:
        spec = ExecSpec(
            argv=["/bin/true"],
            cwd="/workspace",
            sandbox=SandboxSpec(enabled=True, workspace="/workspace", runtime_files=[FileRule("edit", "/etc/resolv.conf")]),
        )

        with self.assertRaises(ValueError):
            build_command(spec)

    def test_build_command_rejects_cwd_outside_workspace(self) -> None:
        spec = ExecSpec(argv=["/bin/true"], cwd="/app", sandbox=SandboxSpec(enabled=True, workspace="/workspace"))

        with self.assertRaises(ValueError):
            build_command(spec)

    def test_build_command_does_not_fail_open_when_file_rules_are_empty(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            spec = ExecSpec(argv=["/bin/true"], cwd=workspace, sandbox=SandboxSpec(enabled=True, workspace=workspace))

            command = build_command(spec)

            self.assertNotIn(f"--bind-try {workspace} {workspace}", " ".join(command.argv))

    def test_build_command_requires_session_id_for_write_rules(self) -> None:
        with tempfile.TemporaryDirectory() as session_root:
            spec = ExecSpec(
                argv=["/bin/true"],
                cwd="/workspace",
                sandbox=SandboxSpec(
                    enabled=True,
                    workspace="/workspace",
                    session_root=session_root,
                    files=[FileRule("write", "/workspace/generated")],
                ),
            )

            with self.assertRaises(ValueError):
                build_command(spec)

    def test_apply_session_writes_copies_new_files_and_allows_session_edits(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_root, tempfile.TemporaryDirectory() as session_root:
            workspace = Path(workspace_root)
            generated_path = workspace / "generated"
            generated_path.mkdir()
            spec = _write_spec(workspace, generated_path, Path(session_root), "agent-session")
            source = Path(session_write_source(session_root, "agent-session", str(workspace), str(generated_path)))
            source.mkdir(parents=True)
            (source / "created.txt").write_text("first", encoding="utf-8")

            apply_session_writes(spec)
            self.assertEqual((generated_path / "created.txt").read_text(encoding="utf-8"), "first")

            (source / "created.txt").write_text("edited", encoding="utf-8")
            apply_session_writes(spec)
            self.assertEqual((generated_path / "created.txt").read_text(encoding="utf-8"), "edited")

    def test_apply_session_writes_rejects_existing_host_file_not_created_by_session(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_root, tempfile.TemporaryDirectory() as session_root:
            workspace = Path(workspace_root)
            generated_path = workspace / "generated"
            generated_path.mkdir()
            host_path = generated_path / "preexisting.txt"
            host_path.write_text("host", encoding="utf-8")
            spec = _write_spec(workspace, generated_path, Path(session_root), "agent-session")
            source = Path(session_write_source(session_root, "agent-session", str(workspace), str(generated_path)))
            source.mkdir(parents=True)
            (source / "preexisting.txt").write_text("session", encoding="utf-8")

            with self.assertRaises(ValueError):
                apply_session_writes(spec)
            self.assertEqual(host_path.read_text(encoding="utf-8"), "host")

    def test_apply_session_writes_allows_last_win_for_managed_host_path(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_root, tempfile.TemporaryDirectory() as session_root:
            workspace = Path(workspace_root)
            generated_path = workspace / "generated"
            generated_path.mkdir()
            specs = [
                _make_session_output(workspace, generated_path, Path(session_root), "agent-a", "a"),
                _make_session_output(workspace, generated_path, Path(session_root), "agent-b", "b"),
            ]
            errors: list[BaseException] = []

            def apply(spec: ExecSpec) -> None:
                try:
                    apply_session_writes(spec)
                except BaseException as error:
                    errors.append(error)

            threads = [threading.Thread(target=apply, args=(spec,)) for spec in specs]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertIn((generated_path / "race.txt").read_text(encoding="utf-8"), {"a", "b"})

    def test_parse_exec_spec_accepts_wire_shape(self) -> None:
        spec = parse_exec_spec(
            {
                "argv": ["/bin/true"],
                "sandbox": {
                    "enabled": True,
                    "files": [{"action": "read", "path": "/workspace"}],
                    "runtime_files": [{"action": "read", "path": "/etc/hosts"}],
                },
            }
        )

        self.assertEqual(spec.argv, ["/bin/true"])
        self.assertTrue(spec.sandbox.enabled)
        self.assertEqual(spec.sandbox.files, [FileRule("read", "/workspace")])
        self.assertEqual(spec.sandbox.runtime_files, [FileRule("read", "/etc/hosts")])

    def test_parse_exec_spec_rejects_string_argv(self) -> None:
        with self.assertRaises(ValueError):
            parse_exec_spec({"argv": "/bin/true"})

    def test_parse_exec_spec_rejects_non_string_env(self) -> None:
        with self.assertRaises(ValueError):
            parse_exec_spec({"argv": ["/bin/true"], "env": {"TOKEN": 123}})

    def test_parse_exec_spec_rejects_invalid_file_rule_shape(self) -> None:
        with self.assertRaises(ValueError):
            parse_exec_spec({"argv": ["/bin/true"], "sandbox": {"files": ["bad"]}})


class _StringReader:
    def __init__(self, value: str) -> None:
        self.value = value

    def read(self, *_args: object) -> str:
        return self.value


def _write_spec(workspace: Path, generated_path: Path, session_root: Path, session_id: str) -> ExecSpec:
    return ExecSpec(
        argv=["/bin/true"],
        sandbox=SandboxSpec(
            enabled=True,
            workspace=str(workspace),
            session_id=session_id,
            session_root=str(session_root),
            files=[FileRule("write", str(generated_path))],
        ),
    )


def _make_session_output(workspace: Path, generated_path: Path, session_root: Path, session_id: str, content: str) -> ExecSpec:
    spec = _write_spec(workspace, generated_path, session_root, session_id)
    source = Path(session_write_source(str(session_root), session_id, str(workspace), str(generated_path)))
    source.mkdir(parents=True)
    (source / "race.txt").write_text(content, encoding="utf-8")
    return spec


if __name__ == "__main__":
    unittest.main()
