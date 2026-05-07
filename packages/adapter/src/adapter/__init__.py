"""Backend adapters used by the mcon system runtime."""

from .anthropic import AnthropicAdapter, AdapterError, RawAdapterResponse
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .copilot import CopilotAdapter
from .nvidia import NvidiaNimAdapter

# Anthropic-compatible direct API backend.
BACKEND_ANTHROPIC = "anthropic"

# Claude Code CLI backend.
BACKEND_CLAUDE_CODE = "claude-code"

# Codex CLI backend.
BACKEND_CODEX = "codex"

# GitHub Copilot CLI backend.
BACKEND_COPILOT = "copilot"

# NVIDIA NIM Chat Completions backend.
BACKEND_NVIDIA = "nvidia"


def build_adapter(
    backend: str = BACKEND_ANTHROPIC,
    *,
    credentials: dict[str, str] | None = None,
    model: str | None = None,
    claude_code_model: str | None = None,
    codex_model: str | None = None,
    copilot_model: str | None = None,
    nvidia_model: str | None = None,
) -> AnthropicAdapter | ClaudeCodeAdapter | CodexAdapter | CopilotAdapter | NvidiaNimAdapter:
    """Create a backend adapter for the selected provider.

    Args:
        backend: Backend identifier from the supported backend constants.
        credentials: Optional credential values loaded from the vault.
        model: Generic model override for the selected backend.
        claude_code_model: Claude Code-specific model override.
        codex_model: Codex-specific model override.
        copilot_model: Copilot-specific model override.
        nvidia_model: NVIDIA-specific model override.

    Returns:
        Adapter instance implementing the backend request methods.

    Raises:
        AdapterError: When the backend name is unsupported.
    """

    normalized_backend = backend.strip().lower()
    if normalized_backend == BACKEND_ANTHROPIC:
        return AnthropicAdapter(credentials=credentials)
    if normalized_backend in (BACKEND_CLAUDE_CODE, "claude"):
        return ClaudeCodeAdapter(credentials=credentials, default_model=model or claude_code_model)
    if normalized_backend == BACKEND_CODEX:
        return CodexAdapter(credentials=credentials, default_model=model or codex_model)
    if normalized_backend == BACKEND_COPILOT:
        return CopilotAdapter(credentials=credentials, default_model=model or copilot_model)
    if normalized_backend in (BACKEND_NVIDIA, "nim", "nvidia-nim"):
        return NvidiaNimAdapter(credentials=credentials, default_model=model or nvidia_model)
    raise AdapterError(503, "configuration_error", f"Unsupported backend: {backend}")


__all__ = [
    "AdapterError",
    "AnthropicAdapter",
    "BACKEND_ANTHROPIC",
    "BACKEND_CLAUDE_CODE",
    "BACKEND_CODEX",
    "BACKEND_COPILOT",
    "BACKEND_NVIDIA",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CopilotAdapter",
    "NvidiaNimAdapter",
    "RawAdapterResponse",
    "build_adapter",
]
