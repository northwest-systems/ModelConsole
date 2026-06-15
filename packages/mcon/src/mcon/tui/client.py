"""Minimal stdin/stdout TUI used for early behavior checks."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def run_tui(server_url: str) -> int:
    print(f"mcon tui -> {server_url}")
    print("commands: ! <argv...> explains command policy, /quit exits")
    while True:
        try:
            line = input("mcon> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {"/quit", "/exit", "quit", "exit"}:
            return 0
        if line.startswith("!"):
            argv = line[1:].strip().split()
            if not argv:
                continue
            try:
                response = _post(
                    server_url,
                    "/api/policy/explain-command",
                    {"subject": "mcon.agent.coder", "argv": argv},
                )
            except RuntimeError as error:
                print(f"error: {error}")
                continue
            final = response["final"]
            print(f"{final['action']}: {final.get('permission', final.get('reason', ''))}")
            continue
        print("stream: fake runtime adapter is not wired yet")


def _post(server_url: str, path: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        f"{server_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        raise RuntimeError(body) from error
    if not isinstance(decoded, dict):
        raise RuntimeError("server returned non-object JSON")
    return decoded
