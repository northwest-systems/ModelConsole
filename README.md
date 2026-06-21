# ModelConsole

Claude Code / Codex 系 runtime を、policy 管理された sandbox で動かすための実験的な console 環境。

## MVP Scope

初期実装では `ignore` 権限を扱わず、file permission は以下に限定する。

- `deny`
- `read`
- `write`
- `edit`

file / network / env の強制は Go executor 側の namespace / mount / netns 実装で行う。command 実行だけ server が argv / semantics を parse して `allow` / `deny` / `ask` を判定する。権限は `mcon.agent.<name>` の subject ごとに解決される。

## Development

Python は container 内外とも必ず `uv` 経由で実行する。

```sh
UV_CACHE_DIR="$PWD/.cache/uv" uv run --no-project python -m unittest discover -s tests
GOCACHE="$PWD/.cache/go-build" go test ./cmd/mcon-executor
PYTHONPATH=packages/mcon/src UV_CACHE_DIR="$PWD/.cache/uv" uv run --no-project python -m mcon explain mcon.agent.coder -- git push origin main
```

Docker では host workspace 全体を container 上の `/workspace` に mount する。server / executor / Codex から見た作業対象も `/workspace` に統一する。

`write` は host workspace への直接 writable bind ではなく、container 内の session fs を対象 path に bind する。command が成功すると executor が session fs の生成物を host workspace へ同期する。host 側に既存 path がある場合は、mcon の workspace manifest に記録済みの managed path だけ上書きできる。これにより、新規作成は host に反映され、mcon が作ったファイルは agent 単位で last win になるが、元から host にあったファイルは上書きできない。別 agent は同じ `session_id` を指定しても別の session fs になる。

`write` の対象 directory は bwrap の mountpoint として host workspace 側に存在している必要がある。MVP では `/workspace/generated` を outbox として用意している。

## Docker

配布単位は Docker image とする。image 内には uv、Python control plane、Go executor、bubblewrap、nvm 管理の Node/npm、Codex CLI を含める。

Docker は配布と外側の隔離に使う。実行ごとの file / network 強制は container 内の Go executor が `bubblewrap` で行う。`bubblewrap` は kernel namespace を使うため、Docker Desktop などでは host の native platform で動かす。cross-arch emulation は開発確認用に限定する。

Node/npm は nvm で pin した最小 runtime として入れる。ModelConsole 自身の `package.json` / `package-lock.json` には runtime に必要な依存だけを記載する。mcon 上で作成するアプリや作業対象の npm 依存は、作業対象側で別途管理する。

```sh
docker build --target test -t modelconsole/mcon:test .
docker build --target runtime -t modelconsole/mcon:dev .
docker compose up --build mcon
```

`docker-compose.yml` は MVP 開発用に `privileged: true` を使う。後続 phase では mount namespace / network namespace に必要な capability へ絞る。

### Codex Device Login

Codex の ChatGPT/OAuth 認証は device login を使う。認証情報は Docker image には焼き込まず、Compose の named volume `codex-home` に保存する。

```sh
docker build --target runtime -t modelconsole/mcon:dev .
docker compose up --build mcon
```

別 terminal から server endpoint を叩いて device login を開始する。

```sh
curl -s -X POST http://127.0.0.1:8765/api/auth/codex/device-login
```

response の `id` を使ってログを poll する。表示された URL をブラウザで開き、one-time code を入力する。

```sh
curl -s http://127.0.0.1:8765/api/auth/codex/device-login/<id>
```

ログイン状態の確認:

```sh
curl -s http://127.0.0.1:8765/api/auth/codex/status
```

ログイン後、同じ `codex-home` volume を使って Codex を実行できる。

```sh
docker compose exec mcon codex login status

docker compose exec mcon \
  uv run --no-project python -m mcon codex-exec "このリポジトリの構成を短く説明して"
```

低レベルに直接確認する場合:

```sh
docker compose exec mcon \
  codex exec --json "このリポジトリの構成を短く説明して"
```

`codex-home` volume には access token が保存されるため、export したり commit したりしない。

### TUI

TUI は server を起動した状態で別 terminal から入る。

```sh
docker compose exec mcon \
  uv run --no-project python -m mcon tui --server http://127.0.0.1:8765
```

通常入力は mcon の orchestration chat stream に送る。TUI は各 user message に `#01234` 形式の chat id を付け、応答待ち中も次の入力を受け付ける。server の `/api/chat/stream` は provider、chat id、解決済みの mcon policy context を使って実行先を決める。現時点の provider は `codex` のみで、Codex プロセス全体を mcon executor の bubblewrap namespace 内で起動し、stdout/stderr を NDJSON として中継する。

stream のファイル mount は agent subject の file policy から生成する。chat は `read-only` profile で実行するため、`edit` は読み取り専用へ縮退し、`write` 専用 path は mask される。provider API 通信は `purpose=provider` の network policy が `inherit` を許可した subject にだけ与える。通常の `/api/exec` は `purpose=command` なので、request から `network=inherit` を指定しても対応する policy がなければ拒否される。

Codex 認証情報は、実行ごとに `/mcon/provider-runtime` へ必要ファイルだけをコピーする。コピーを Codex runtime home として namespace 内へ mountし、Codexが最初のイベントを返した時点で認証ファイルを削除する。runtime directory自体も実行終了時に削除する。元の`codex-home` volumeはnamespaceへ直接mountしない。

TUI commands:

```text
! git status              # explain command policy
/exec git status --short  # run through mcon executor policy/sandbox
/provider codex
/subject mcon.agent.auditor
/cwd /workspace/some-repo
/session session-a
/sandbox read-only
/clear
/status
/quit
```

TTY では入力行の Backspace、Delete、Ctrl-U、Ctrl-W、Ctrl-A/Ctrl-E、左右矢印、Home/End を扱う。応答が割り込んだ場合も入力中の行を再描画する。

`/sandbox workspace-write` は、Codex の内部 tool 実行を mcon executor に完全転送できるまで chat stream では拒否される。通常は `read-only` のまま使う。workspace への変更が必要な command は `/exec` で mcon executor policy/sandbox を通して実行する。

Container 内の health check:

```sh
docker run --rm -d --name mcon-smoke \
  -p 127.0.0.1:18765:8765 \
  -v "$PWD:/workspace:rw" \
  --privileged \
  modelconsole/mcon:dev

docker exec mcon-smoke uv run --no-project python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8765/health").read().decode())'
docker stop mcon-smoke
```

### Executor Sandbox

Go executor は初期 sandbox spec を受け取れる。`sandbox.enabled=true` の場合、`bubblewrap` で namespace / mount 制限を作る。

例:

```json
{
  "argv": ["/usr/bin/env"],
  "cwd": "/workspace",
  "env": {
    "PATH": "/usr/local/bin:/usr/bin:/bin"
  },
  "sandbox": {
    "enabled": true,
    "workspace": "/workspace",
    "network": "none",
    "session_id": "mcon_agent_coder--session-1",
    "session_root": "/mcon/session-fs",
    "files": [
      {"action": "read", "path": "/workspace"},
      {"action": "edit", "path": "/workspace/docs"},
      {"action": "write", "path": "/workspace/generated"},
      {"action": "deny", "path": "/workspace/secrets"}
    ]
  }
}
```

`network: "none"` は `--unshare-net` を使う。file rule は `read -> ro-bind-try`, `edit -> bind-try`, `write -> session fs bind + post-run host apply`, `deny -> tmpfs mask` に変換される。server は subject の file policy からこの sandbox spec を生成する。

Server 経由で実行する場合、server は command policy を先に判定し、`allow` された command だけを `mcon-executor` に渡す。executor へ渡す環境変数は request の `env` と最小 `PATH` に限定され、server process の環境変数は実行対象へ継承しない。request の `session_id` は server 側で agent subject と結合されるため、agent 間で writable session fs は共有されない。
