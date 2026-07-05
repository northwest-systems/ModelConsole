"""Run and secretless agent workspace management."""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class RunState(StrEnum):
    CREATED = "CREATED"
    SYNCING_IN = "SYNCING_IN"
    READY = "READY"
    QUARANTINED = "QUARANTINED"
    DISCARDED = "DISCARDED"


SECRET_FILE_NAMES = {
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
}
SECRET_DIR_NAMES = {
    ".aws",
    ".gcp",
    ".azure",
    ".ssh",
    "secrets",
}
SECRET_CONTENT_MARKERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "AWS_SECRET_ACCESS_KEY",
)


@dataclass
class RunRecord:
    run_id: str
    subject: str
    real_workspace: Path
    run_root: Path
    agent_workspace: Path
    baseline_workspace: Path
    state: RunState = RunState.CREATED
    policy_snapshot: dict[str, Any] = field(default_factory=dict)
    quarantine_reason: str | None = None
    baseline: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "subject": self.subject,
            "state": self.state.value,
            "real_workspace": str(self.real_workspace),
            "agent_workspace": str(self.agent_workspace),
            "policy_snapshot": self.policy_snapshot,
            "quarantine_reason": self.quarantine_reason,
        }


class RunService:
    def __init__(self, *, root: Path, default_workspace: Path) -> None:
        self.root = root
        self.default_workspace = default_workspace
        self.runs: dict[str, RunRecord] = {}

    def create_run(
        self,
        *,
        subject: str,
        policy_snapshot: dict[str, Any],
        workspace: Path | None = None,
    ) -> RunRecord:
        real_workspace = (workspace or self.default_workspace).resolve()
        if not real_workspace.exists() or not real_workspace.is_dir():
            raise ValueError(f"workspace must be an existing directory: {real_workspace}")
        run_id = self._new_run_id(subject, real_workspace)
        run_root = self.root / run_id
        agent_workspace = run_root / "workspace"
        baseline_workspace = run_root / "baseline"
        record = RunRecord(
            run_id=run_id,
            subject=subject,
            real_workspace=real_workspace,
            run_root=run_root,
            agent_workspace=agent_workspace,
            baseline_workspace=baseline_workspace,
            policy_snapshot=policy_snapshot,
        )
        run_root.mkdir(parents=True, exist_ok=False)
        self.runs[run_id] = record
        return record

    def get(self, run_id: str) -> RunRecord:
        try:
            return self.runs[run_id]
        except KeyError as error:
            raise ValueError(f"unknown run_id: {run_id}") from error

    def sync_in(self, run_id: str) -> RunRecord:
        record = self.get(run_id)
        if record.state == RunState.QUARANTINED:
            raise ValueError(f"run is quarantined: {record.quarantine_reason}")
        record.state = RunState.SYNCING_IN
        if record.agent_workspace.exists():
            shutil.rmtree(record.agent_workspace)
        if record.baseline_workspace.exists():
            shutil.rmtree(record.baseline_workspace)
        record.agent_workspace.mkdir(parents=True)
        self._copy_secretless(record.real_workspace, record.agent_workspace)
        findings = self.scan_workspace(record.agent_workspace)
        if findings:
            record.state = RunState.QUARANTINED
            record.quarantine_reason = f"secret material detected: {findings[0]}"
            return record
        shutil.copytree(record.agent_workspace, record.baseline_workspace)
        record.baseline = self._hash_tree(record.agent_workspace)
        record.state = RunState.READY
        return record

    def diff(self, run_id: str) -> dict[str, Any]:
        record = self.get(run_id)
        findings = self.scan_workspace(record.agent_workspace)
        if findings:
            record.state = RunState.QUARANTINED
            record.quarantine_reason = f"secret material detected: {findings[0]}"
            return {
                "run_id": run_id,
                "state": record.state.value,
                "changed": [],
                "patch": "",
                "quarantine_reason": record.quarantine_reason,
            }
        current = self._hash_tree(record.agent_workspace)
        paths = sorted(set(record.baseline) | set(current))
        changed = [path for path in paths if record.baseline.get(path) != current.get(path)]
        return {
            "run_id": run_id,
            "state": record.state.value,
            "changed": changed,
            "patch": self._unified_diff(record, changed),
            "quarantine_reason": record.quarantine_reason,
        }

    def discard(self, run_id: str) -> RunRecord:
        record = self.get(run_id)
        if record.run_root.exists():
            shutil.rmtree(record.run_root)
        record.state = RunState.DISCARDED
        return record

    def resolve_cwd(self, run_id: str, requested: str | None) -> Path:
        record = self.get(run_id)
        if record.state != RunState.READY:
            raise ValueError(f"run must be READY before command execution: {record.state.value}")
        if requested is None:
            return record.agent_workspace
        candidate = Path(requested)
        if not candidate.is_absolute():
            resolved = (record.agent_workspace / candidate).resolve()
        else:
            resolved_input = candidate.resolve()
            if resolved_input == record.real_workspace or record.real_workspace in resolved_input.parents:
                resolved = record.agent_workspace / resolved_input.relative_to(record.real_workspace)
            else:
                resolved = resolved_input
        if resolved != record.agent_workspace and record.agent_workspace not in resolved.parents:
            raise ValueError(f"cwd must be inside agent workspace: {resolved}")
        return resolved

    def scan_workspace(self, root: Path) -> list[str]:
        findings: list[str] = []
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            reason = secret_path_reason(relative)
            if reason:
                findings.append(f"{relative}: {reason}")
                continue
            if path.is_file() and _file_contains_secret_marker(path):
                findings.append(f"{relative}: secret-like content")
        return findings

    def _copy_secretless(self, source: Path, destination: Path) -> None:
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            if secret_path_reason(relative) or _is_git_metadata(relative):
                if path.is_dir():
                    continue
                continue
            target = destination / relative
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                continue
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target, follow_symlinks=False)

    def _hash_tree(self, root: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        if not root.exists():
            return result
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            result[relative] = _sha256(path)
        return result

    def _unified_diff(self, record: RunRecord, changed: list[str]) -> str:
        chunks: list[str] = []
        for relative in changed:
            before_path = record.baseline_workspace / relative
            after_path = record.agent_workspace / relative
            before = _read_text_lines(before_path)
            after = _read_text_lines(after_path)
            if before is None or after is None:
                chunks.append(f"Binary files differ: {relative}\n")
                continue
            chunks.extend(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                    lineterm="",
                )
            )
            if chunks and chunks[-1] != "":
                chunks.append("")
        return "\n".join(chunks)

    def _new_run_id(self, subject: str, workspace: Path) -> str:
        digest = hashlib.sha256(f"{subject}:{workspace}:{os.urandom(16).hex()}".encode("utf-8")).hexdigest()
        return "run_" + digest[:24]


def secret_path_reason(relative: Path) -> str | None:
    parts = [part.lower() for part in relative.parts]
    if not parts:
        return None
    name = parts[-1]
    if name == ".env" or name.startswith(".env."):
        return "environment file"
    if name in SECRET_FILE_NAMES:
        return "credential file"
    if any(part in SECRET_DIR_NAMES for part in parts):
        return "secret directory"
    if any("secret" in part or "credential" in part for part in parts):
        return "secret-like path"
    return None


def _is_git_metadata(relative: Path) -> bool:
    return bool(relative.parts) and relative.parts[0] == ".git"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_contains_secret_marker(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if b"\x00" in data:
        return False
    text = data[:1024 * 1024].decode("utf-8", errors="ignore")
    return any(marker in text for marker in SECRET_CONTENT_MARKERS)


def _read_text_lines(path: Path) -> list[str] | None:
    if not path.exists():
        return []
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.splitlines()
