"""Session-oriented system runtime used by mcon frontends."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import adapter
import middleware

from audit import AuditLogger, utc_now_iso
from auth import load_cli_auth_environment
from config import RuntimeConfig

# backend ごとの標準 model。boot --model や MCON_*_MODEL で上書きされる前の初期値。
DEFAULT_MODEL_BY_BACKEND = {
    "anthropic": "claude-3-5-sonnet-latest",
    "claude-code": "claude-sonnet-4-6",
    "codex": "gpt-5.3-codex",
    "copilot": "gpt-5.3-codex",
    "nvidia": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
}


@dataclass(frozen=True)
class SessionSummary:
    """Small session summary returned to frontend clients."""

    id: str
    title: str
    backend: str
    model: str
    created_at: str
    updated_at: str
    message_count: int


class SystemRuntime:
    """In-process runtime for sessions, transcript logs, and backend calls."""

    def __init__(self, runtime_config: RuntimeConfig) -> None:
        """system runtime の保存先と audit logger を初期化する。

        Args:
            runtime_config: data directory、backend、audit root などの runtime 設定。

        Returns:
            なし。
        """

        self.runtime_config = runtime_config
        self.sessions_root = runtime_config.data_directory / "sessions"
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.audit_logger = AuditLogger(runtime_config.audit_root)

    def create_session(
        self,
        *,
        title: str | None = None,
        backend: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """session metadata と transcript file を作成する。

        Args:
            title: session title。未指定なら session id から生成する。
            backend: 使用する backend 名。未指定なら config default。
            model: 使用する model 名。未指定なら backend default。

        Returns:
            作成した session summary。
        """

        normalized_backend = _normalize_backend(backend or self.runtime_config.backend)
        selected_model = model or _default_model(normalized_backend)
        session_id = uuid.uuid4().hex
        now = utc_now_iso()
        session_directory = self._session_directory(session_id)
        session_directory.mkdir(parents=True, exist_ok=False)
        metadata = {
            "id": session_id,
            "title": title or f"session-{session_id[:8]}",
            "backend": normalized_backend,
            "model": selected_model,
            "created_at": now,
            "updated_at": now,
        }
        self._write_metadata(session_id, metadata)
        self._transcript_path(session_id).touch()
        self.audit_logger.write_event(
            "session_event",
            {"event": "created", "session_id": session_id, "backend": normalized_backend, "model": selected_model},
        )
        return self.get_session(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        """保存済み session の一覧を取得する。

        Args:
            なし。

        Returns:
            更新日時の降順に並べた session summary の list。
        """

        sessions: list[dict[str, Any]] = []
        for metadata_path in self.sessions_root.glob("*/session.json"):
            try:
                session = self.get_session(metadata_path.parent.name)
            except (OSError, ValueError, KeyError):
                continue
            sessions.append(session)
        return sorted(sessions, key=lambda item: str(item["updated_at"]), reverse=True)

    def get_session(self, session_id: str) -> dict[str, Any]:
        """session id から session summary を取得する。

        Args:
            session_id: 取得する session id。

        Returns:
            session summary。
        """

        metadata = self._read_metadata(session_id)
        transcript = self.read_transcript(session_id)
        summary = SessionSummary(
            id=str(metadata["id"]),
            title=str(metadata["title"]),
            backend=str(metadata["backend"]),
            model=str(metadata["model"]),
            created_at=str(metadata["created_at"]),
            updated_at=str(metadata["updated_at"]),
            message_count=len(transcript),
        )
        return summary.__dict__

    def read_transcript(self, session_id: str) -> list[dict[str, Any]]:
        """session transcript を JSONL から読み込む。

        Args:
            session_id: transcript を読む session id。

        Returns:
            transcript message の list。
        """

        transcript_path = self._transcript_path(session_id)
        if not transcript_path.exists():
            raise KeyError(session_id)
        messages: list[dict[str, Any]] = []
        for line in transcript_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            messages.append(json.loads(line))
        return messages

    def append_user_message(self, session_id: str, content: str) -> dict[str, Any]:
        """user message を追加し、backend response を assistant message として保存する。

        Args:
            session_id: message を追加する session id。
            content: user message text。

        Returns:
            更新後 session、user message、assistant message、backend 情報を含む dict。
        """

        if not content.strip():
            raise ValueError("message content must not be empty")
        metadata = self._read_metadata(session_id)
        user_message = _transcript_message("user", content)
        self._append_transcript_message(session_id, user_message)
        request_payload = self._build_provider_request(session_id, metadata)
        backend_name = str(metadata["backend"])
        started_at = utc_now_iso()
        try:
            backend_adapter = self._build_backend_adapter(backend_name, str(metadata["model"]))
            middleware_runner = self._build_middleware()
            middleware_response = middleware_runner.forward(
                middleware.MiddlewareRequest(
                    session_id=session_id,
                    backend=backend_name,
                    model=str(metadata["model"]),
                    payload=request_payload,
                    headers={},
                ),
                backend_adapter,
            )
            response_payload = middleware_response.payload
            status_code = middleware_response.status_code
            context_mode = middleware_response.context_mode
        except adapter.AdapterError as adapter_error:
            self.audit_logger.write_event(
                "system_error",
                {
                    "session_id": session_id,
                    "backend": backend_name,
                    "status": adapter_error.status_code,
                    "message": adapter_error.message,
                },
            )
            raise

        assistant_text = _assistant_text_from_response(response_payload)
        assistant_message = _transcript_message("assistant", assistant_text)
        self._append_transcript_message(session_id, assistant_message)
        updated_metadata = {**metadata, "updated_at": assistant_message["ts"]}
        self._write_metadata(session_id, updated_metadata)
        usage = response_payload.get("usage", {}) if isinstance(response_payload, dict) else {}
        self.audit_logger.write_event(
            "system_llm_call",
            {
                "session_id": session_id,
                "backend": backend_name,
                "model": request_payload["model"],
                "status": status_code,
                "started_at": started_at,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "context_mode": context_mode,
                "message_count": len(request_payload["messages"]),
            },
        )
        return {
            "session": self.get_session(session_id),
            "user_message": user_message,
            "assistant_message": assistant_message,
            "backend": backend_name,
            "model": request_payload["model"],
            "status": status_code,
        }

    def _build_provider_request(self, session_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        """transcript 全体を backend adapter 用 request に変換する。

        Args:
            session_id: request を作る session id。
            metadata: session metadata。

        Returns:
            adapter に渡す Anthropic-like request payload。
        """

        transcript = self.read_transcript(session_id)
        messages = [{"role": item["role"], "content": item["content"]} for item in transcript if item["role"] in {"user", "assistant"}]
        return {
            "model": str(metadata["model"]),
            "max_tokens": 4096,
            "stream": False,
            "messages": messages,
        }

    def _build_backend_adapter(self, backend: str, model: str) -> Any:
        """backend 名と model から adapter instance を作る。

        Args:
            backend: provider backend 名。
            model: 使用する model 名。

        Returns:
            `forward_json` を持つ backend adapter instance。
        """

        credentials = load_cli_auth_environment(self.runtime_config.vault_root, _credential_target(backend))
        environment_credentials = {key: value for key, value in os.environ.items() if key.endswith("API_KEY") or key.endswith("TOKEN")}
        return adapter.build_adapter(backend, credentials={**environment_credentials, **credentials}, model=model)

    def _build_middleware(self) -> middleware.BaseMiddleware:
        """system と adapter の間に挟む middleware instance を作る。

        Args:
            なし。

        Returns:
            request/response を処理する middleware instance。標準では bypass。
        """

        return middleware.build_middleware()

    def _session_directory(self, session_id: str) -> Path:
        """session id から session directory path を作る。

        Args:
            session_id: path に変換する session id。

        Returns:
            session directory path。
        """

        if not session_id or "/" in session_id or "\\" in session_id:
            raise KeyError(session_id)
        return self.sessions_root / session_id

    def _metadata_path(self, session_id: str) -> Path:
        """session metadata file path を返す。

        Args:
            session_id: 対象 session id。

        Returns:
            `session.json` の path。
        """

        return self._session_directory(session_id) / "session.json"

    def _transcript_path(self, session_id: str) -> Path:
        """session transcript file path を返す。

        Args:
            session_id: 対象 session id。

        Returns:
            `transcript.jsonl` の path。
        """

        return self._session_directory(session_id) / "transcript.jsonl"

    def _read_metadata(self, session_id: str) -> dict[str, Any]:
        """session metadata を読み込む。

        Args:
            session_id: 読み込む session id。

        Returns:
            session metadata dict。
        """

        metadata_path = self._metadata_path(session_id)
        if not metadata_path.exists():
            raise KeyError(session_id)
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    def _write_metadata(self, session_id: str, metadata: dict[str, Any]) -> None:
        """session metadata を保存する。

        Args:
            session_id: 保存先 session id。
            metadata: 保存する metadata dict。

        Returns:
            なし。
        """

        self._metadata_path(session_id).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _append_transcript_message(self, session_id: str, message: dict[str, Any]) -> None:
        """transcript JSONL に message を追記する。

        Args:
            session_id: 追記先 session id。
            message: 追記する transcript message。

        Returns:
            なし。
        """

        with self._transcript_path(session_id).open("a", encoding="utf-8") as transcript_file:
            transcript_file.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")


def _transcript_message(role: str, content: str) -> dict[str, Any]:
    """transcript message object を作る。

    Args:
        role: message role。
        content: message text。

    Returns:
        id と timestamp を含む transcript message dict。
    """

    return {"id": uuid.uuid4().hex, "ts": utc_now_iso(), "role": role, "content": content}


def _normalize_backend(backend: str) -> str:
    """backend 名を内部表現に正規化する。

    Args:
        backend: user/config 由来の backend 名。

    Returns:
        正規化済み backend 名。
    """

    normalized_backend = backend.strip().lower()
    if normalized_backend == "claude":
        return "claude-code"
    return "nvidia" if normalized_backend in {"nim", "nvidia-nim"} else normalized_backend


def _default_model(backend: str) -> str:
    """backend の default model を返す。

    Args:
        backend: backend 名。

    Returns:
        default model 名。未知 backend の場合は Anthropic default。
    """

    return DEFAULT_MODEL_BY_BACKEND.get(backend, DEFAULT_MODEL_BY_BACKEND["anthropic"])


def _credential_target(backend: str) -> Any:
    """backend 名から credential target 名へ変換する。

    Args:
        backend: backend 名。

    Returns:
        `auth.load_cli_auth_environment` に渡す target 名。
    """

    if backend in {"anthropic", "claude-code"}:
        return "claude"
    if backend in {"codex", "copilot", "nvidia"}:
        return backend
    return "claude"


def _assistant_text_from_response(response_payload: dict[str, Any]) -> str:
    """adapter response から assistant text を取り出す。

    Args:
        response_payload: adapter が返した message payload。

    Returns:
        TUI と transcript に保存する assistant text。
    """

    content = response_payload.get("content", [])
    if isinstance(content, str):
        return content
    text_parts: list[str] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
    return "\n".join(part for part in text_parts if part).strip()
