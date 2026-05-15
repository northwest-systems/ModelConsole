# PERMISSIONS

## Overview

mcon は FastAPI server を中心に permission を管理する。

UI / TUI は server に接続する。

agent runtime と tool runtime は、session ごとの Unix domain socket で server に接続する。

command 実行は server 経由のみとする。

server は Policy Manager で `allow` / `deny` / `ask` を判定する。

`allow` の場合、server または executor helper が runtime context 内で `execve` する。

agent runtime と tool runtime からの直接 `execve` / `execveat` は制限する。

## Runtime Architecture

mcon は Docker container 上で server を起動する。

container の root は host と同義の権限として扱う。

FastAPI server は管理用ユーザーで動作する。

capability が必要な namespace / mount / network / seccomp / ownership 操作は executor helper が行う。

agent は session ごとに runtime を持つ。

agent runtime は subprocess または threading で起動する。

agent runtime は OAuth / API 認証済みプロセス、または one-shot 実行を wrapper で扱う。

agent ごとに process と namespace を分ける。

container は session ごとには作らない。

## Session Socket

session ごとに Unix domain socket を作成する。

socket path は以下の形式とする。

```text
/run/mcon/sessions/<session_id>/server.sock
```

agent runtime と tool runtime は、自分の session socket に接続する。

server は socket path から session を識別する。

message 内に session id が含まれる場合でも、server は socket context の session を正とする。

並列 session は socket 単位で分離する。

session 終了時、server は session socket と session directory を削除する。

## Plugin Permission Model

mcon は plugin 単位で permission を定義する。

plugin は複数の policy を持つ。

agent と tool は plugin に属する capability である。

agent と tool は `uses` で policy を複数指定する。

policy は permission の集合であり、agent や tool の sandbox を定義する。

plugin は以下を持つ。

- policy
- agent
- tool
- skill

policy は以下の permission を持てる。

- `commands`
- `credentials`
- `networks`
- `files`

## Fully Qualified Name

plugin 内の要素は、以下の形式で一意に表す。

```text
<plugin>.<kind>.<name>
```

例:

```text
mcon.policy.git
mcon.agent.coder
mcon.tool.websearch
```

同じ plugin 内で参照する場合は短縮名を使える。

```toml
uses = ["base", "git"]
```

plugin をまたぐ参照を許可する場合は、完全修飾名を使う。

```toml
uses = ["mcon.policy.base", "github.policy.readonly"]
```

## Configuration

plugin 設定は TOML で記述する。

```toml
[plugin]
name = "mcon"
description = "Built-in mcon plugin."

include = [
  "policies/base.toml",
  "policies/git.toml",
  "policies/websearch.toml",
  "policies/edit.toml",
  "agents/coder.toml",
  "tools/websearch.toml",
  "tools/edit.toml",
]
```

`include` は plugin root からの相対パスとする。

include は記載順に読み込む。

同じ完全修飾名が複数回定義された場合は、後から読み込まれた定義を採用する。

## Schema

以下は設定構造の例である。

```toml
# plugin root の設定。
[plugin]
name = "mcon"
description = "Built-in mcon plugin."

# plugin root からの相対パス。
# 記載順に読み込む。
include = [
  "policies/base.toml",
  "policies/git.toml",
  "agents/coder.toml",
  "tools/websearch.toml",
]

# policy 定義。
# policy 名は plugin 内で一意。
[policy.git]
description = "Git command permissions."

# command permission。
# table key の git-status は permission key。
[policy.git.commands.git-status]
description = "Allow git status."
action = "allow"
command = "git"
subcommands = ["status"]

# command 実行時に渡す credential key。
uses = []

# command option の条件。
[policy.git.commands.git-status.options]
# これらの option が含まれる場合に match する。
include = []

# これらの option が含まれる場合に match しない。
exclude = []

# option 全体を正規表現で絞る。
pattern = []

# command argument の条件。
[policy.git.commands.git-status.args]
# argv の引数部分を正規表現で絞る。
pattern = []

# command-specific parser の解析結果。
[policy.git.commands.git-status.semantics]
# parser ごとの項目を置く。

# credential permission。
# table key の github-token が credential store の参照 key。
[policy.git.credentials.github-token]
description = "GitHub token."

# process に渡す環境変数名。
as = "GITHUB_TOKEN"

# network permission。
[policy.git.networks.github]
description = "Allow GitHub network access."
action = "allow"
domains = ["github.com", "api.github.com"]

# file permission。
[policy.git.files.repo-read]
description = "Allow repository read."
action = "read"
paths = ["/workspace/repo"]

# agent 定義。
[agent.coder]
description = "General coding agent."

# policy 名の配列。
# 後ろの policy が前の policy を上書きする。
uses = ["base", "git"]

# tool 定義。
[tool.websearch]
description = "Managed web search."

# none または caller。
inherit = "none"

# tool が使う policy 名の配列。
uses = ["base", "websearch-runtime"]
```

## Policy Resolution

`uses` は配列順に解決する。

```toml
uses = [
  "base",
  "git",
  "no-main-push",
]
```

この場合、以下の順に読み込む。

```text
base -> git -> no-main-push
```

後ろの policy が前の policy を上書きする。

これを `last win` と呼ぶ。

`allow` / `deny` / `ask` は、最後に match した permission の action を採用する。

解決済みポリシーとは、`uses` で指定された policy を順番に読み込み、`last win` を適用した結果である。

## Policy Manager

Policy Manager は policy を読み込み、実行可否を判定する。

Policy Manager は以下を行う。

1. TOML を読み込む
2. include を展開する
3. plugin / policy / agent / tool を解決する
4. `uses` を配列順に解決する
5. 解決済みポリシーを作る
6. request を permission に照合する
7. `allow` / `deny` / `ask` を返す

`ask` は一回限りの承認とする。

承認された `ask` は、その request だけ `allow` として扱う。

Policy Manager は実行を行わない。

実行は server または executor helper が行う。

## Command Permission

command permission は process として起動する command の許可を表す。

`action` は以下を使用する。

- `allow`
- `deny`
- `ask`

例:

```toml
[policy.git.commands.git-status]
description = "Allow git status."
action = "allow"
command = "git"
subcommands = ["status"]

[policy.git.commands.git-push]
description = "Ask before git push."
action = "ask"
command = "git"
subcommands = ["push"]
uses = ["github-token"]

[policy.git.commands.git-push-main]
description = "Deny push to protected branches."
action = "deny"
command = "git"
subcommands = ["push"]

[policy.git.commands.git-push-main.semantics]
target_branches = ["main", "master"]
```

`options` は command option の条件を表す。

```toml
[policy.npm.commands.npm-install]
description = "Allow local npm install."
action = "allow"
command = "npm"
subcommands = ["install"]

[policy.npm.commands.npm-install.options]
exclude = ["--global", "-g"]
```

`args.pattern` は command argument を正規表現で絞る。

```toml
[policy.aws.commands.aws-s3-ls]
description = "Allow aws s3 ls for staging buckets."
action = "allow"
command = "aws"
subcommands = ["s3", "ls"]

[policy.aws.commands.aws-s3-ls.args]
pattern = ["^s3://stg-[a-z0-9-]+(/.*)?$"]
```

`semantics` は command-specific parser の解析結果を表す。

`git push origin HEAD:main` のように argv だけでは意味が分かりにくいものは、parser が target branch などへ正規化する。

command の `uses` は、command 実行時に渡す credential key の配列である。

server は command を実行ファイルへ解決し、`execve` で実行する。

```text
execve(
  "/usr/bin/git",
  ["git", "push", "origin", "feature/foo"],
  base_env + credential_env
)
```

credential は `envp` として渡される。

## Server Mediated Command Execution

agent runtime と tool runtime は command を直接実行しない。

command 実行は server に依頼する。

server は command request を Policy Manager に渡す。

Policy Manager が `allow` を返した場合のみ、server または executor helper が `execve` する。

agent runtime と tool runtime からの直接 `execve` / `execveat` は制限する。

初期実装では、server 経由の command 実行だけを正規経路とする。

## Agent Command Flow

agent が command を実行する時、agent は server に command 実行依頼を送る。

command 実行依頼は、少なくとも以下を含む。

```text
agent: mcon.agent.coder
cwd: /workspace/repo
argv: ["git", "push", "origin", "feature/foo"]
```

実行の流れ:

```text
1. agent が command 実行依頼を server に送る
2. server が agent の uses を解決する
3. server が command を実行ファイルへ解決する
4. server が argv を解析する
5. server が command permission を評価する
6. server が最後に match した action を採用する
7. action が ask の場合は user approval を取得する
8. server が command.uses の credential を解決する
9. server が credential を env overlay に変換する
10. server が execve で command を実行する
```

## Credential Permission

credential permission は command 実行時に env overlay として渡す credential を定義する。

```toml
[policy.git.credentials.github-token]
description = "GitHub token."
as = "GITHUB_TOKEN"
```

credential の table key が credential store の参照 key である。

```text
credential store key: github-token
env key:              GITHUB_TOKEN
```

credential value は実行時に credential store から参照する。

`as` は process に渡す環境変数名である。

credential store は container 停止後も残る private storage に保存する。

credential store は外部プロセスから直接参照されない。

実行ログでは credential value を mask する。

credential key と env key は実行ログに記録できる。

起動済み process の env は更新しない。

credential store が更新された場合、次回以降の command 実行に適用する。

## Network Permission

network permission は network 管理層に渡す許可を表す。

```toml
[policy.git.networks.github]
description = "Allow GitHub network access."
action = "allow"
domains = [
  "github.com",
  "api.github.com",
]
```

network 管理層は、network permission を使って通信を制御する。

network 管理層は subdomain 単位で通信を制御する。

runtime の direct egress は network permission に従う。

## File Permission

file permission は workspace file へのアクセス許可を表す。

```toml
[policy.git.files.repo-read]
description = "Allow repository read."
action = "read"
paths = ["/workspace/repo"]

[policy.git.files.repo-edit]
description = "Allow repository edit."
action = "edit"
paths = ["/workspace/repo"]
```

file permission の action は以下を使用する。

- `ignore`
- `read`
- `write`
- `edit`
- `deny`

file 管理層は、file permission を使って workspace file へのアクセスを制御する。

file 管理層は tmpfs と self mount を使い、session ごとの file view を構成する。

`read` は read-only mount として適用する。

`edit` は read-write mount として適用する。

`write` は許可された directory への新規作成を許可する。

`write` では既存ファイルを read-only として扱う。

agent が `write` で新規 file を作成した場合、server は作成直後に owner を server に渡す。

作成した session には、その file への一時 `edit` を付与する。

session 終了時、server は policy を再適用し、一時 `edit` を削除する。

## Tool

tool は plugin に属する custom command である。

tool は user または agent から実行される。

tool は `inherit` で、agent から呼ばれた時に caller の policy を反映するかを指定する。

`inherit` は以下を使用する。

- `none`: tool の解決済みポリシーで実行する
- `caller`: caller の解決済みポリシーと tool の解決済みポリシーの共通部分で実行する

user が tool を直接実行する場合は、tool の解決済みポリシーで実行する。

共通部分とは、caller と tool の両方で許可されている範囲である。

例えば caller が `/workspace/repo` を `read`、tool が `/workspace/repo` を `edit` としている場合、共通部分は `read` である。

### Agent Tool Flow

agent が tool を実行する時、agent は server に tool 実行依頼を送る。

tool 実行依頼は、少なくとも以下を含む。

```text
agent: mcon.agent.coder
tool: mcon.tool.websearch
input: "latest Node.js LTS"
```

実行の流れ:

```text
1. agent が tool 実行依頼を server に送る
2. server が agent の uses を解決する
3. server が tool の uses を解決する
4. server が tool.inherit を確認する
5. inherit = none の場合は tool の解決済みポリシーを final policy とする
6. inherit = caller の場合は agent と tool の解決済みポリシーの共通部分を final policy とする
7. server が final policy を持つ tool runtime を起動する
8. tool runtime が command を実行する場合、command 実行依頼を server に送る
9. server が final policy で command を実行ファイルへ解決する
10. server が argv を解析する
11. server が final policy の command permission を評価する
12. server が最後に match した action を採用する
13. action が ask の場合は user approval を取得する
14. server が command.uses の credential を解決する
15. server が credential を env overlay に変換する
16. server が execve で command を実行する
```

### User Tool Flow

user が tool を直接実行する時、server は tool の policy だけを使う。

```text
1. user が tool 実行依頼を server に送る
2. server が tool の uses を解決する
3. server が tool の解決済みポリシーを final policy として tool runtime を起動する
4. tool runtime が command を実行する場合、command 実行依頼を server に送る
5. server が final policy で command を実行ファイルへ解決する
6. server が argv を解析する
7. server が final policy の command permission を評価する
8. server が最後に match した action を採用する
9. action が ask の場合は user approval を取得する
10. server が command.uses の credential を解決する
11. server が credential を env overlay に変換する
12. server が execve で command を実行する
```

### Websearch Example

websearch は、agent の policy を継承せずに実行する tool として定義する。

```toml
[tool.websearch]
description = "Managed web search."
inherit = "none"

uses = [
  "base",
  "websearch-runtime",
]

[policy.websearch-runtime.networks.web]
description = "Allow websearch network."
action = "allow"
domains = ["*"]
```

`inherit = "none"` により、websearch は tool の解決済みポリシーで実行される。

### Edit Example

edit は、agent の policy を継承して実行する tool として定義する。

```toml
[tool.edit]
description = "Direct workspace editor."
inherit = "caller"

uses = [
  "base",
  "edit-runtime",
]

[policy.edit-runtime.files.workspace-edit]
description = "Allow direct workspace edits."
action = "edit"
paths = ["/workspace/repo"]
```

`inherit = "caller"` により、agent 主導の edit は agent と edit tool の file permission の共通部分で実行される。

## Executor Helper

server は policy 解決と承認を行う。

executor helper は runtime context の構築と実行を行う。

executor helper は以下を扱う。

- namespace
- mount
- network setup
- credential env
- seccomp
- ownership
- `execve`

capability が必要な操作は executor helper が行う。

## Validation

以下を設定エラーとする。

- include の循環
- 存在しない policy 参照
- 存在しない credential key 参照
- `as` のない credential
- 同一 policy 内の permission key 重複
- 不明な action
- 不明な inherit
- 不正な正規表現
- session socket と subject の不一致

## Explain

`last win` による最終判断を説明できるようにする。

```text
mcon explain mcon.agent.coder "git push origin main"

matched:
  policy.git.commands.git-push       action=ask
  policy.git.commands.git-push-main  action=deny

final:
  deny by policy.git.commands.git-push-main
```
