"""Policy manager for command and file access decisions."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


COMMAND_ACTIONS = {"allow", "deny", "ask"}
FILE_ACTIONS = {"deny", "read", "write", "edit"}
TOOL_INHERIT = {"none", "caller"}


class PolicyError(ValueError):
    """Raised when policy configuration is invalid."""


@dataclass(frozen=True)
class CommandPermission:
    policy_name: str
    key: str
    action: str
    command: str
    subcommands: tuple[str, ...] = ()
    uses: tuple[str, ...] = ()
    option_include: tuple[str, ...] = ()
    option_exclude: tuple[str, ...] = ()
    option_patterns: tuple[re.Pattern[str], ...] = ()
    arg_patterns: tuple[re.Pattern[str], ...] = ()
    semantics: dict[str, Any] = field(default_factory=dict)

    @property
    def fqn(self) -> str:
        return f"{self.policy_name}.commands.{self.key}"


@dataclass(frozen=True)
class FilePermission:
    policy_name: str
    key: str
    action: str
    paths: tuple[str, ...]

    @property
    def fqn(self) -> str:
        return f"{self.policy_name}.files.{self.key}"


@dataclass(frozen=True)
class CredentialPermission:
    policy_name: str
    key: str
    env_name: str


@dataclass
class Policy:
    name: str
    commands: list[CommandPermission] = field(default_factory=list)
    files: list[FilePermission] = field(default_factory=list)
    credentials: dict[str, CredentialPermission] = field(default_factory=dict)


@dataclass(frozen=True)
class Subject:
    kind: str
    name: str
    uses: tuple[str, ...]
    inherit: str | None = None


@dataclass(frozen=True)
class ResolvedPolicy:
    subject: str
    policy_names: tuple[str, ...]
    commands: tuple[CommandPermission, ...]
    files: tuple[FilePermission, ...]
    credentials: dict[str, CredentialPermission]


class PolicyManager:
    """Loads plugin policies and evaluates access decisions."""

    def __init__(self, *, plugin_name: str, policies: dict[str, Policy], agents: dict[str, Subject], tools: dict[str, Subject]) -> None:
        self.plugin_name = plugin_name
        self.policies = policies
        self.agents = agents
        self.tools = tools

    @classmethod
    def load(cls, plugin_root: Path) -> "PolicyManager":
        root_path = plugin_root / "plugin.toml"
        if not root_path.exists():
            raise PolicyError(f"plugin.toml not found: {root_path}")
        loader = _PluginLoader(plugin_root)
        document = loader.load(root_path)
        plugin = document.get("plugin")
        if not isinstance(plugin, dict):
            raise PolicyError("plugin table is required")
        plugin_name = _required_string(plugin, "name", "plugin")

        policies = _parse_policies(plugin_name, document.get("policy", {}))
        agents = _parse_subjects(plugin_name, "agent", document.get("agent", {}))
        tools = _parse_subjects(plugin_name, "tool", document.get("tool", {}))
        manager = cls(plugin_name=plugin_name, policies=policies, agents=agents, tools=tools)
        manager.validate()
        return manager

    def validate(self) -> None:
        for subject in [*self.agents.values(), *self.tools.values()]:
            self._resolve_policy_names(subject.uses)

        for policy in self.policies.values():
            credential_keys = set(policy.credentials)
            for command in policy.commands:
                unknown = sorted(set(command.uses) - credential_keys)
                if unknown:
                    raise PolicyError(f"{command.fqn} uses unknown credential(s): {', '.join(unknown)}")

    def explain_command(self, subject_name: str, argv: list[str]) -> dict[str, Any]:
        resolved = self.resolve_subject(subject_name)
        matched: list[dict[str, Any]] = []
        final: CommandPermission | None = None
        for permission in resolved.commands:
            if _command_matches(permission, argv):
                matched.append(
                    {
                        "permission": permission.fqn,
                        "action": permission.action,
                        "uses": list(permission.uses),
                    }
                )
                final = permission

        if final is None:
            return {
                "subject": resolved.subject,
                "argv": argv,
                "matched": matched,
                "final": {"action": "deny", "reason": "no matching command permission"},
            }
        return {
            "subject": resolved.subject,
            "argv": argv,
            "matched": matched,
            "final": {
                "action": final.action,
                "permission": final.fqn,
                "uses": list(final.uses),
            },
        }

    def explain_file(self, subject_name: str, operation: str, path: str) -> dict[str, Any]:
        resolved = self.resolve_subject(subject_name)
        matched: list[dict[str, Any]] = []
        final: FilePermission | None = None
        normalized_path = _normalize_posix_path(path)
        for permission in resolved.files:
            for configured_path in permission.paths:
                if _path_contains(configured_path, normalized_path):
                    matched.append(
                        {
                            "permission": permission.fqn,
                            "action": permission.action,
                            "path": configured_path,
                        }
                    )
                    final = permission

        if final is None:
            return {
                "subject": resolved.subject,
                "operation": operation,
                "path": normalized_path,
                "matched": matched,
                "final": {"action": "deny", "allowed": False, "reason": "no matching file permission"},
            }
        allowed = _file_action_allows(final.action, operation)
        return {
            "subject": resolved.subject,
            "operation": operation,
            "path": normalized_path,
            "matched": matched,
            "final": {
                "action": final.action,
                "allowed": allowed,
                "permission": final.fqn,
            },
        }

    def sandbox_spec(
        self,
        subject_name: str,
        *,
        workspace: str = "/workspace",
        network: str = "none",
        session_id: str | None = None,
        session_root: str | None = None,
    ) -> dict[str, Any]:
        resolved = self.resolve_subject(subject_name)
        normalized_workspace = _normalize_posix_path(workspace)
        files: list[dict[str, str]] = []
        for permission in resolved.files:
            for path in permission.paths:
                if not _path_contains(normalized_workspace, path):
                    raise PolicyError(f"{permission.fqn} path must be inside workspace for sandboxing: {path}")
                files.append({"action": permission.action, "path": path})
        return {
            "enabled": True,
            "workspace": normalized_workspace,
            "network": network,
            "session_id": session_id,
            "session_root": session_root,
            "files": files,
        }

    def resolve_subject(self, subject_name: str) -> ResolvedPolicy:
        subject = self._subject_for_name(subject_name)
        policy_names = self._resolve_policy_names(subject.uses)
        commands: list[CommandPermission] = []
        files: list[FilePermission] = []
        credentials: dict[str, CredentialPermission] = {}
        for policy_name in policy_names:
            policy = self.policies[policy_name]
            commands.extend(policy.commands)
            files.extend(policy.files)
            credentials.update(policy.credentials)
        return ResolvedPolicy(
            subject=f"{self.plugin_name}.{subject.kind}.{subject.name}",
            policy_names=tuple(policy_names),
            commands=tuple(commands),
            files=tuple(files),
            credentials=credentials,
        )

    def _subject_for_name(self, subject_name: str) -> Subject:
        parts = subject_name.split(".")
        if len(parts) == 3:
            plugin, kind, name = parts
            if plugin != self.plugin_name:
                raise PolicyError(f"unknown plugin in subject: {subject_name}")
        elif len(parts) == 2:
            kind, name = parts
        else:
            kind, name = "agent", subject_name

        if kind == "agent" and name in self.agents:
            return self.agents[name]
        if kind == "tool" and name in self.tools:
            return self.tools[name]
        raise PolicyError(f"unknown subject: {subject_name}")

    def _resolve_policy_names(self, uses: tuple[str, ...]) -> list[str]:
        policy_names: list[str] = []
        for policy_ref in uses:
            policy_name = self._normalize_policy_ref(policy_ref)
            if policy_name not in self.policies:
                raise PolicyError(f"unknown policy: {policy_ref}")
            policy_names.append(policy_name)
        return policy_names

    def _normalize_policy_ref(self, policy_ref: str) -> str:
        parts = policy_ref.split(".")
        if len(parts) == 3:
            plugin, kind, name = parts
            if plugin != self.plugin_name or kind != "policy":
                raise PolicyError(f"unsupported cross-plugin policy reference: {policy_ref}")
            return name
        return policy_ref


class _PluginLoader:
    def __init__(self, plugin_root: Path) -> None:
        self.plugin_root = plugin_root
        self.loading: list[Path] = []
        self.loaded: set[Path] = set()

    def load(self, path: Path) -> dict[str, Any]:
        resolved = path.resolve()
        if resolved in self.loading:
            cycle = " -> ".join(str(item) for item in [*self.loading, resolved])
            raise PolicyError(f"include cycle detected: {cycle}")
        if resolved in self.loaded:
            return {}

        self.loading.append(resolved)
        with resolved.open("rb") as handle:
            document = tomllib.load(handle)
        merged: dict[str, Any] = {}
        plugin = document.get("plugin")
        if isinstance(plugin, dict):
            for include_path in plugin.get("include", []):
                included = self.load((self.plugin_root / include_path).resolve())
                _deep_merge(merged, included)
        _deep_merge(merged, document)
        self.loading.pop()
        self.loaded.add(resolved)
        return merged


def _parse_policies(plugin_name: str, raw_policies: object) -> dict[str, Policy]:
    if not isinstance(raw_policies, dict):
        raise PolicyError("policy table must be an object")
    policies: dict[str, Policy] = {}
    for policy_key, raw_policy in raw_policies.items():
        if not isinstance(raw_policy, dict):
            raise PolicyError(f"policy.{policy_key} must be an object")
        policy = Policy(name=str(policy_key))
        policy_name = f"{plugin_name}.policy.{policy_key}"
        for key, raw_command in _table_items(raw_policy.get("commands", {}), f"policy.{policy_key}.commands"):
            policy.commands.append(_parse_command(policy_name, key, raw_command))
        for key, raw_file in _table_items(raw_policy.get("files", {}), f"policy.{policy_key}.files"):
            policy.files.append(_parse_file(policy_name, key, raw_file))
        for key, raw_credential in _table_items(raw_policy.get("credentials", {}), f"policy.{policy_key}.credentials"):
            env_name = _required_string(raw_credential, "as", f"policy.{policy_key}.credentials.{key}")
            policy.credentials[key] = CredentialPermission(policy_name=policy_name, key=key, env_name=env_name)
        policies[policy_key] = policy
    return policies


def _parse_command(policy_name: str, key: str, raw_command: dict[str, Any]) -> CommandPermission:
    action = _required_string(raw_command, "action", key)
    if action not in COMMAND_ACTIONS:
        raise PolicyError(f"{key} has invalid command action: {action}")
    options = raw_command.get("options", {})
    args = raw_command.get("args", {})
    if not isinstance(options, dict):
        raise PolicyError(f"{key}.options must be an object")
    if not isinstance(args, dict):
        raise PolicyError(f"{key}.args must be an object")
    return CommandPermission(
        policy_name=policy_name,
        key=key,
        action=action,
        command=_required_string(raw_command, "command", key),
        subcommands=tuple(_strings(raw_command.get("subcommands", []), f"{key}.subcommands")),
        uses=tuple(_strings(raw_command.get("uses", []), f"{key}.uses")),
        option_include=tuple(_strings(options.get("include", []), f"{key}.options.include")),
        option_exclude=tuple(_strings(options.get("exclude", []), f"{key}.options.exclude")),
        option_patterns=tuple(_compile_patterns(options.get("pattern", []), f"{key}.options.pattern")),
        arg_patterns=tuple(_compile_patterns(args.get("pattern", []), f"{key}.args.pattern")),
        semantics=dict(raw_command.get("semantics", {})),
    )


def _parse_file(policy_name: str, key: str, raw_file: dict[str, Any]) -> FilePermission:
    action = _required_string(raw_file, "action", key)
    if action not in FILE_ACTIONS:
        raise PolicyError(f"{key} has invalid file action: {action}")
    paths = tuple(_normalize_posix_path(item) for item in _strings(raw_file.get("paths", []), f"{key}.paths"))
    if not paths:
        raise PolicyError(f"{key} must define at least one path")
    return FilePermission(policy_name=policy_name, key=key, action=action, paths=paths)


def _parse_subjects(plugin_name: str, kind: str, raw_subjects: object) -> dict[str, Subject]:
    if raw_subjects is None:
        return {}
    if not isinstance(raw_subjects, dict):
        raise PolicyError(f"{kind} table must be an object")
    subjects: dict[str, Subject] = {}
    for subject_key, raw_subject in raw_subjects.items():
        if not isinstance(raw_subject, dict):
            raise PolicyError(f"{kind}.{subject_key} must be an object")
        inherit = raw_subject.get("inherit")
        if inherit is not None and inherit not in TOOL_INHERIT:
            raise PolicyError(f"{kind}.{subject_key} has invalid inherit: {inherit}")
        subjects[subject_key] = Subject(
            kind=kind,
            name=subject_key,
            uses=tuple(_strings(raw_subject.get("uses", []), f"{kind}.{subject_key}.uses")),
            inherit=inherit,
        )
    return subjects


def _command_matches(permission: CommandPermission, argv: list[str]) -> bool:
    if not argv or argv[0] != permission.command:
        return False
    if permission.subcommands and tuple(argv[1 : 1 + len(permission.subcommands)]) != permission.subcommands:
        return False
    if any(option not in argv for option in permission.option_include):
        return False
    if any(option in argv for option in permission.option_exclude):
        return False
    option_args = [item for item in argv[1:] if item.startswith("-")]
    if any(not any(pattern.search(option) for option in option_args) for pattern in permission.option_patterns):
        return False
    trailing_args = argv[1 + len(permission.subcommands) :]
    if any(not any(pattern.search(arg) for arg in trailing_args) for pattern in permission.arg_patterns):
        return False
    target_branches = permission.semantics.get("target_branches")
    if target_branches is not None:
        parsed = _parse_git_target_branches(argv)
        return bool(set(_strings(target_branches, f"{permission.fqn}.semantics.target_branches")) & parsed)
    return True


def _parse_git_target_branches(argv: list[str]) -> set[str]:
    if len(argv) < 2 or argv[0] != "git" or argv[1] != "push":
        return set()
    targets: set[str] = set()
    for item in argv[2:]:
        if item.startswith("-") or item == "origin":
            continue
        branch = item.rsplit(":", 1)[-1]
        if "/" in branch:
            branch = branch.rsplit("/", 1)[-1]
        if branch:
            targets.add(branch)
    return targets


def _file_action_allows(action: str, operation: str) -> bool:
    matrix = {
        "deny": set(),
        "read": {"stat", "list", "read"},
        "write": {"create"},
        "edit": {"stat", "list", "read", "create", "write"},
    }
    return operation in matrix[action]


def _path_contains(configured_path: str, requested_path: str) -> bool:
    configured = configured_path.rstrip("/")
    requested = requested_path.rstrip("/")
    return requested == configured or requested.startswith(configured + "/")


def _normalize_posix_path(path: str) -> str:
    if not path:
        raise PolicyError("path must not be empty")
    pure_path = PurePosixPath(path)
    if not pure_path.is_absolute():
        raise PolicyError(f"path must be absolute: {path}")
    return pure_path.as_posix()


def _required_string(table: dict[str, Any], key: str, context: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{context}.{key} must be a non-empty string")
    return value


def _strings(value: object, context: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PolicyError(f"{context} must be a list of strings")
    return value


def _compile_patterns(value: object, context: str) -> list[re.Pattern[str]]:
    patterns = []
    for pattern in _strings(value, context):
        try:
            patterns.append(re.compile(pattern))
        except re.error as error:
            raise PolicyError(f"{context} contains invalid regex {pattern!r}: {error}") from error
    return patterns


def _table_items(value: object, context: str) -> list[tuple[str, dict[str, Any]]]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise PolicyError(f"{context} must be an object")
    items: list[tuple[str, dict[str, Any]]] = []
    for key, raw in value.items():
        if not isinstance(raw, dict):
            raise PolicyError(f"{context}.{key} must be an object")
        items.append((str(key), raw))
    return items


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
