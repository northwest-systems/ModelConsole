"""Provider authentication jobs for the mcon server."""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcon.adapters import codex

ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class AuthProviderError(ValueError):
    """Raised for unsupported or invalid auth-provider operations."""


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    device_login_command: tuple[str, ...]
    status_command: tuple[str, ...]
    environment: dict[str, str]
    cwd: Path


class DeviceLoginJob:
    """Background process used for device-code login."""

    def __init__(self, *, provider: str, command: list[str], environment: dict[str, str], cwd: Path) -> None:
        self.id = uuid.uuid4().hex
        self.provider = provider
        self.command = command
        self.environment = environment
        self.cwd = cwd
        self.status = "pending"
        self.returncode: int | None = None
        self.started_at = time.time()
        self.updated_at = self.started_at
        self._output: deque[str] = deque(maxlen=200)
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread = threading.Thread(target=self._run, name=f"auth-{provider}-{self.id[:8]}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "provider": self.provider,
                "status": self.status,
                "returncode": self.returncode,
                "command": self.command,
                "output": "".join(self._output),
                "started_at": self.started_at,
                "updated_at": self.updated_at,
            }

    def _run(self) -> None:
        master_fd: int | None = None
        slave_fd: int | None = None
        try:
            master_fd, slave_fd = pty.openpty()
            self._set_status("running")
            self._process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self.environment,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
            slave_fd = None
            self._read_until_exit(master_fd)
            self.returncode = self._process.wait()
            self._set_status("succeeded" if self.returncode == 0 else "failed")
        except FileNotFoundError as error:
            self._append_output(f"{error}\n")
            self.returncode = 127
            self._set_status("failed")
        except Exception as error:  # pragma: no cover - defensive status capture
            self._append_output(f"{type(error).__name__}: {error}\n")
            self.returncode = 1
            self._set_status("failed")
        finally:
            if slave_fd is not None:
                os.close(slave_fd)
            if master_fd is not None:
                os.close(master_fd)

    def _read_until_exit(self, master_fd: int) -> None:
        assert self._process is not None
        while self._process.poll() is None:
            ready, _, _ = select.select([master_fd], [], [], 0.25)
            if ready:
                self._read_once(master_fd)
        self._read_once(master_fd)

    def _read_once(self, master_fd: int) -> None:
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            return
        if data:
            self._append_output(data.decode("utf-8", errors="replace"))

    def _append_output(self, text: str) -> None:
        with self._lock:
            self._output.append(_clean_terminal_output(text))
            self.updated_at = time.time()

    def _set_status(self, status: str) -> None:
        with self._lock:
            self.status = status
            self.updated_at = time.time()


class AuthManager:
    """Starts and tracks provider auth flows."""

    def __init__(self, providers: dict[str, ProviderSpec]) -> None:
        self.providers = providers
        self.jobs: dict[str, DeviceLoginJob] = {}
        self._lock = threading.Lock()

    @classmethod
    def default(cls) -> "AuthManager":
        environment = codex.codex_environment(os.environ.items())
        provider = ProviderSpec(
            name="codex",
            device_login_command=("codex", "login", "--device-auth"),
            status_command=("codex", "login", "status"),
            environment=environment,
            cwd=codex.workspace_path(),
        )
        return cls({"codex": provider})

    def list_providers(self) -> dict[str, Any]:
        return {
            "providers": [
                {
                    "name": provider.name,
                    "supports_device_login": True,
                }
                for provider in self.providers.values()
            ]
        }

    def start_device_login(self, provider_name: str) -> dict[str, Any]:
        provider = self._provider(provider_name)
        job = DeviceLoginJob(
            provider=provider.name,
            command=list(provider.device_login_command),
            environment=provider.environment,
            cwd=provider.cwd,
        )
        with self._lock:
            self.jobs[job.id] = job
        job.start()
        return job.snapshot()

    def get_device_login(self, provider_name: str, job_id: str) -> dict[str, Any]:
        self._provider(provider_name)
        with self._lock:
            job = self.jobs.get(job_id)
        if job is None or job.provider != provider_name:
            raise AuthProviderError(f"unknown device login job: {job_id}")
        return job.snapshot()

    def status(self, provider_name: str) -> dict[str, Any]:
        provider = self._provider(provider_name)
        try:
            result = subprocess.run(
                provider.status_command,
                cwd=provider.cwd,
                env=provider.environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError:
            return {
                "provider": provider.name,
                "available": False,
                "authenticated": False,
                "status": "codex command not found",
            }
        output = (result.stdout + result.stderr).strip()
        return {
            "provider": provider.name,
            "available": True,
            "authenticated": result.returncode == 0,
            "returncode": result.returncode,
            "status": output,
        }

    def _provider(self, provider_name: str) -> ProviderSpec:
        provider = self.providers.get(provider_name)
        if provider is None:
            raise AuthProviderError(f"unsupported auth provider: {provider_name}")
        return provider


def redacted_environment(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {key: value for key, value in items if not key.endswith("TOKEN") and not key.endswith("KEY")}


def _clean_terminal_output(text: str) -> str:
    return ANSI_PATTERN.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
