# ModelConsole Test Requirements

この文書は、現行の Python / uv 実装に対するテスト要件と、正常系・異常系の網羅状況を記録する。

## Review Stance

テストは、プロンプトインジェクションや悪意あるコード混入を前提に、次の失敗を検出できる必要がある。

- real workspace や host path への意図しない直接アクセス
- secret file / secret-like content の Agent Workspace 混入
- path traversal、symlink 経由の workspace 脱出
- command / file / network policy の fail-open
- provider process や child command の権限境界の崩れ
- session fs apply による既存 host file の破壊
- raw executor spec の型ゆるみ、環境変数混入、runtime mount allowlist bypass

## Requirement Matrix

| Area | Requirement | Normal Cases | Abnormal / Adversarial Cases | Coverage Status |
|---|---|---|---|---|
| Policy command | Last matching command rule decides final action. | `git status`, feature branch `git push` ask. | Protected branch push denied, unknown subject/policy denied. | Covered |
| Policy file | File action matrix is enforced after path normalization. | workspace read, generated create, docs edit. | `/workspace/../etc/passwd` rejected, denied secret paths rejected. | Covered |
| Policy network | `none` is allowed, `inherit` is purpose-scoped. | provider purpose can inherit. | command purpose cannot inherit. | Covered |
| Sandbox spec | Resolved file policy becomes deterministic bwrap rules. | read/write/edit/deny rules are emitted in order. | command network inherit rejected, read-only profile downgrades writes. | Covered |
| Executor spec parsing | Executor accepts only typed JSON wire shapes. | argv/env/stdin/sandbox file rules parse. | string argv, non-string env, malformed file rules rejected. | Covered |
| Executor bwrap | Sandbox command is always wrapped with bwrap and constrained mounts. | read, edit, write, deny, runtime read mounts are emitted. | cwd outside workspace, file rule outside workspace, unsupported network, runtime path outside allowlist rejected. | Covered |
| Executor runtime mounts | Runtime files are narrow allowlist only. | provider runtime and codex package paths allowed. | Codex credential home read, writable `/etc`, unsupported runtime action rejected. | Covered |
| Executor session fs apply | `write` paths use session fs and managed manifest. | New file copy, managed file edit, concurrent managed last-win. | Existing unmanaged host file overwrite rejected, symlink output rejected, host symlink parent rejected. | Covered |
| Run sync-in | Agent Workspace is secretless and detached from `.git`. | normal files copied. | `.env`, `.npmrc`, `.ssh`, `secrets`, `.git`, symlinks excluded. | Covered |
| Run quarantine | Secret-like content stops the run before agent-visible diff. | safe workspace becomes READY, safe diff produced. | post-sync secret content and diff-time secret content quarantine with empty patch. | Covered |
| Run cwd mapping | Commands can map real workspace paths into Agent Workspace. | real workspace cwd maps to agent cwd. | outside cwd and non-ready run rejected. | Covered |
| HTTP Run API | Public API exposes run creation, sync-in, diff, discard behavior. | create + sync-in + diff works over HTTP. | diff-time secret content returns quarantined response with empty patch. | Covered by opt-in socket integration test plus RunService unit tests |
| Provider chat | Parent provider is read-only and shell-disabled. | prompt includes policy context and MCP command instruction. | workspace-write rejected, MCP token required, unknown tool rejected. | Covered |
| Process lifecycle | Provider process cleanup is deterministic. | runtime credential copy removed after first event. | broken client write terminates process group; surviving children receive SIGKILL. | Covered |

## Adversarial Review Result

Before this test pass, the main gaps were:

- executor path handling had normal bwrap coverage, but not explicit file-rule-outside-workspace or unsupported-network checks;
- session fs apply had managed overwrite tests, but not symlink output and host symlink parent checks;
- RunService had secret filtering tests, but not `.git` / symlink exclusion and cwd mapping boundaries;
- Run API existed in server routing, but had no end-to-end HTTP test. A socket-based integration test now exists, gated by `MCON_ENABLE_SOCKET_TESTS=1` because some sandboxed environments cannot bind localhost sockets.

Those gaps are now represented as tests. The current residual risk is not a missing unit case but an unimplemented product feature:

- `apply` with user approval and drift check is still not implemented;
- event/audit persistence is still not implemented;
- network proxy/allowlist profiles are still not implemented;
- command broker still accepts raw argv in the MVP path.

Those residual risks should become test requirements when the corresponding feature work lands.

## Required Test Commands

Run unit/integration tests through uv:

```sh
UV_CACHE_DIR="$PWD/.cache/uv" uv run --no-project python -m unittest discover -s tests
```

Run socket-based HTTP integration tests in an environment that allows localhost bind:

```sh
MCON_ENABLE_SOCKET_TESTS=1 UV_CACHE_DIR="$PWD/.cache/uv" \
  uv run --no-project python -m unittest tests.test_server_api
```

Compile-check all Python modules:

```sh
python3 -m compileall packages/mcon/src
```

Run the executor module directly:

```sh
printf '%s' '{"argv":["/bin/sh","-c","printf %s ok"]}' \
  | PYTHONPATH=packages/mcon/src python3 -m mcon.executor
```
